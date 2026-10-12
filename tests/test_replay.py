"""Replay of an action log — planner, locator matcher, runner and tool (linc-nerve-3me).

A fake controller stands in for adb/WDA and a fake clock/sleep pair stands in
for time, so nothing waits for real. Log files live under ``tmp_path``. No test
spawns adb or talks to :8111.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from linc_nerve.core import action_log, replay
from linc_nerve.core.replay import Step, find_element, plan_steps
from linc_nerve.mcp import _core, actionlog_tools, input_tools
from linc_nerve.mcp.actionlog_tools import (
    _nerve_actionlog_start_sync,
    _nerve_actionlog_stop_sync,
    _nerve_replay_sync,
)

NEXT = {
    "class": "android.widget.Button",
    "text": "Next",
    "bounds": "[100,100][300,200]",
}
NEXT_TARGET = {
    "resource_id": "",
    "text": "Next",
    "content_desc": "",
    "class_name": "android.widget.Button",
    "bounds": "[100,1800][980,1950]",
}

FIXTURE_LOG: list[dict[str, Any]] = [
    {"type": "header", "version": 1, "serial": "S", "started_at": "t", "meta": {}},
    {
        "type": "action",
        "seq": 1,
        "tool": "nerve_elements",
        "args": {},
        "duration_ms": 120.0,
        "ok": True,
        "error": None,
        "readonly": True,
        "t_session_s": 0.0,
        "t_rec_s": None,
    },
    {
        "type": "action",
        "seq": 2,
        "tool": "nerve_tap",
        "args": {"x": 540, "y": 1870},
        "duration_ms": 50.0,
        "ok": True,
        "error": None,
        "target": NEXT_TARGET,
        "t_session_s": 1.0,
        "t_rec_s": None,
    },
    {
        "type": "action",
        "seq": 3,
        "tool": "nerve_tap",
        "args": {"x": 10, "y": 10},
        "duration_ms": 40.0,
        "ok": False,
        "error": "Tap failed: boom",
        "target": None,
        "t_session_s": 2.0,
        "t_rec_s": None,
    },
    {
        "type": "action",
        "seq": 4,
        "tool": "nerve_type",
        "args": {"text": "<redacted>", "secret": True, "length": 2},
        "duration_ms": 60.0,
        "ok": True,
        "error": None,
        "t_session_s": 3.0,
        "t_rec_s": None,
    },
    {
        "type": "action",
        "seq": 5,
        "tool": "nerve_key",
        "args": {"key": "BACK"},
        "duration_ms": 30.0,
        "ok": True,
        "error": None,
        "t_session_s": 4.0,
        "t_rec_s": None,
    },
    {"type": "guard", "tool": "nerve_launch", "reason": "package", "detail": "com.other"},
]


class FakeClock:
    """Monotonic time that only moves when ``sleep`` is called."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeController:
    """Records input calls; ``screens`` is consumed one list per elements() poll."""

    def __init__(self, screens: list[list[dict[str, Any]]] | None = None) -> None:
        self.screens = list(screens or [])
        self.last_screen: list[dict[str, Any]] = []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.element_kwargs: list[dict[str, Any]] = []
        self.type_error: Exception | None = None

    def elements(self, serial: str, max_depth: int = 30, force_refresh: bool = False) -> list:
        self.element_kwargs.append({"force_refresh": force_refresh})
        if self.screens:
            self.last_screen = self.screens.pop(0)
        return self.last_screen

    def tap(self, serial: str, x: int, y: int) -> None:
        self.calls.append(("tap", (x, y)))

    def long_press(self, serial: str, x: int, y: int, duration_ms: int = 500) -> None:
        self.calls.append(("long_press", (x, y, duration_ms)))

    def swipe(self, serial: str, sx: int, sy: int, ex: int, ey: int, duration_ms: int) -> None:
        self.calls.append(("swipe", (sx, sy, ex, ey, duration_ms)))

    def type_text(self, serial: str, text: str) -> None:
        if self.type_error is not None:
            raise self.type_error
        self.calls.append(("type_text", (text,)))

    def press_key(self, serial: str, key: str) -> None:
        self.calls.append(("press_key", (key,)))

    def launch(self, serial: str, package: str, uri: str = "", extras: Any = None) -> None:
        self.calls.append(("launch", (package, uri)))


def _run(ctrl: FakeController, steps: list[Step], **kwargs: Any) -> replay.ReplayResult:
    clock = FakeClock()
    kwargs.setdefault("secrets", {})
    return replay.replay(ctrl, "S", steps, clock=clock, sleep=clock.sleep, **kwargs)


def _tap(locator: dict[str, str] | None, x: int = 540, y: int = 1870) -> Step:
    return Step(tool="nerve_tap", args={"x": x, "y": y}, locator=locator, seq=2)


# ---------------------------------------------------------------------------
# plan_steps
# ---------------------------------------------------------------------------


def test_plan_steps_keeps_tap_type_key_in_order() -> None:
    steps = plan_steps(FIXTURE_LOG)
    assert [s.tool for s in steps] == ["nerve_tap", "nerve_type", "nerve_key"]
    tap, typed, key = steps
    assert tap.args == {"x": 540, "y": 1870}
    assert tap.locator == {
        "resource_id": "",
        "text": "Next",
        "content_desc": "",
        "class_name": "android.widget.Button",
    }
    assert typed.locator is None
    assert typed.seq == 4
    assert key.args == {"key": "BACK"}


def test_plan_steps_drops_session_control_tools_and_junk() -> None:
    entries: list[Any] = [
        "not a dict",
        {"type": "action", "seq": 1, "tool": "nerve_actionlog_start", "args": {}, "ok": True},
        {"type": "action", "seq": 2, "tool": "nerve_replay", "args": {}, "ok": True},
        {"type": "action", "seq": 3, "tool": "nerve_guard_start", "args": {}, "ok": True},
        {"type": "action", "seq": 4, "tool": "nerve_record_start", "args": {}, "ok": True},
        {"type": "action", "seq": 5, "tool": "nerve_screenshot", "args": {}, "ok": True},
        {"type": "action", "seq": 6, "tool": "nerve_swipe", "args": {"start_x": 1}, "ok": True},
    ]
    steps = plan_steps(entries)
    assert [s.tool for s in steps] == ["nerve_swipe"]


# ---------------------------------------------------------------------------
# find_element
# ---------------------------------------------------------------------------


def test_find_element_prefers_resource_id_and_text() -> None:
    elements = [
        {"id": "app:id/btn", "text": "Back", "bounds": "[0,0][10,10]"},
        {"id": "app:id/other", "text": "Next", "bounds": "[0,20][10,30]"},
        {"id": "app:id/btn", "text": "Next", "bounds": "[0,40][10,50]"},
    ]
    match = find_element(elements, {"resource_id": "app:id/btn", "text": "Next"})
    assert match is not None and match["bounds"] == "[0,40][10,50]"


def test_find_element_none_when_only_matching_text_is_shared() -> None:
    elements = [
        {"text": "Next", "bounds": "[0,0][10,10]"},
        {"text": "Next", "bounds": "[0,20][10,30]"},
    ]
    assert find_element(elements, {"text": "Next"}) is None


def test_find_element_resource_id_alone_when_unique() -> None:
    elements = [
        {"resource_id": "app:id/go", "text": "Continue", "bounds": "[0,0][10,10]"},
        {"resource_id": "app:id/x", "text": "Next", "bounds": "[0,20][10,30]"},
        {"resource_id": "app:id/y", "text": "Next", "bounds": "[0,40][10,50]"},
    ]
    match = find_element(elements, {"resource_id": "app:id/go", "text": "Next"})
    assert match is not None and match["text"] == "Continue"


def test_find_element_content_desc_and_children() -> None:
    elements = [
        {
            "class": "android.widget.FrameLayout",
            "bounds": "[0,0][100,100]",
            "children": [{"desc": "Open menu", "bounds": "[0,0][10,10]"}],
        }
    ]
    match = find_element(elements, {"content_desc": "Open menu"})
    assert match is not None and match["bounds"] == "[0,0][10,10]"
    assert find_element(elements, {"content_desc": "Close"}) is None
    assert find_element(elements, {"class_name": "android.widget.FrameLayout"}) is None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def test_runner_waits_for_element_then_taps_its_centre() -> None:
    ctrl = FakeController(screens=[[], [], [NEXT]])
    result = _run(ctrl, [_tap(NEXT_TARGET)])
    assert ctrl.calls == [("tap", (200, 150))]
    assert result["outcome"] == "completed"
    assert result["steps_total"] == 1
    assert result["steps_done"] == 1
    assert result["diverged_at"] is None
    assert result["reason"] is None
    assert len(ctrl.element_kwargs) == 3
    assert all(kw == {"force_refresh": True} for kw in ctrl.element_kwargs)


def test_runner_diverges_when_element_never_appears() -> None:
    ctrl = FakeController(screens=[[]])
    result = _run(ctrl, [_tap(NEXT_TARGET)], step_timeout_s=2.0, poll_s=0.5)
    assert result["outcome"] == "diverged"
    assert result["diverged_at"] == 0
    assert result["steps_done"] == 0
    assert "Next" in (result["reason"] or "")
    assert ("tap", (540, 1870)) not in ctrl.calls
    assert ctrl.calls == []
    assert result["duration_s"] == pytest.approx(2.0)


def test_runner_secret_type_uses_secret_by_seq() -> None:
    steps = plan_steps(FIXTURE_LOG)[1:2]
    ctrl = FakeController()
    result = _run(ctrl, steps, secrets={"4": "pw"})
    assert ctrl.calls == [("type_text", ("pw",))]
    assert result["outcome"] == "completed"


def test_runner_missing_secret_diverges() -> None:
    steps = plan_steps(FIXTURE_LOG)[1:2]
    ctrl = FakeController()
    result = _run(ctrl, steps, secrets={})
    assert result["outcome"] == "diverged"
    assert result["diverged_at"] == 0
    assert result["reason"] == "missing secret for step seq 4"
    assert ctrl.calls == []


def test_runner_null_target_tap_replays_recorded_coordinates() -> None:
    ctrl = FakeController()
    result = _run(ctrl, [_tap(None, x=33, y=44)])
    assert ctrl.calls == [("tap", (33, 44))]
    assert ctrl.element_kwargs == []
    assert result["outcome"] == "completed"


def test_runner_content_desc_only_locator_falls_back_to_coordinates() -> None:
    locator = {"resource_id": "", "text": "", "content_desc": "Menu", "class_name": ""}
    ctrl = FakeController(screens=[[]])
    result = _run(ctrl, [_tap(locator, x=7, y=8)], step_timeout_s=1.0)
    assert ctrl.calls == [("tap", (7, 8))]
    assert result["outcome"] == "completed"


def test_runner_class_only_locator_skips_polling() -> None:
    locator = {"resource_id": "", "text": "", "content_desc": "", "class_name": "Frame"}
    ctrl = FakeController()
    _run(ctrl, [_tap(locator, x=7, y=8)])
    assert ctrl.calls == [("tap", (7, 8))]
    assert ctrl.element_kwargs == []


def test_runner_elements_error_counts_as_not_found() -> None:
    class Flaky(FakeController):
        def elements(self, serial: str, **kwargs: Any) -> list:
            raise RuntimeError("uiautomator killed")

    result = _run(Flaky(), [_tap(NEXT_TARGET)], step_timeout_s=1.0)
    assert result["outcome"] == "diverged"


def test_runner_elements_without_force_refresh_parameter() -> None:
    class IOSLike(FakeController):
        def elements(self, serial: str, max_depth: int = 20) -> list:  # type: ignore[override]
            self.element_kwargs.append({})
            return [NEXT]

    ctrl = IOSLike()
    result = _run(ctrl, [_tap(NEXT_TARGET)])
    assert ctrl.calls == [("tap", (200, 150))]
    assert result["outcome"] == "completed"


def test_runner_long_press_located_keeps_duration() -> None:
    step = Step(
        tool="nerve_long_press",
        args={"x": 1, "y": 1, "duration_ms": 1200},
        locator={"text": "Next"},
    )
    ctrl = FakeController(screens=[[NEXT]])
    _run(ctrl, [step])
    assert ctrl.calls == [("long_press", (200, 150, 1200))]


def test_runner_replays_other_tools_with_recorded_args() -> None:
    steps = [
        Step(tool="nerve_launch", args={"package": "com.x", "uri": "x://a"}),
        Step(
            tool="nerve_swipe",
            args={"start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4, "duration_ms": 300},
        ),
        Step(tool="nerve_type", args={"text": "hello"}),
        Step(tool="nerve_key", args={"key": "BACK"}),
    ]
    ctrl = FakeController()
    result = _run(ctrl, steps)
    assert ctrl.calls == [
        ("launch", ("com.x", "x://a")),
        ("swipe", (1, 2, 3, 4, 300)),
        ("type_text", ("hello",)),
        ("press_key", ("BACK",)),
    ]
    assert result == {
        "outcome": "completed",
        "steps_total": 4,
        "steps_done": 4,
        "diverged_at": None,
        "reason": None,
        "duration_s": 0.0,
    }


def test_runner_waits_recorded_gap_capped_at_three_seconds() -> None:
    steps = [
        Step(tool="nerve_key", args={"key": "A"}, t_session_s=0.0, duration_s=0.5),
        Step(tool="nerve_key", args={"key": "B"}, t_session_s=1.5, duration_s=0.1),
        Step(tool="nerve_key", args={"key": "C"}, t_session_s=30.0),
        Step(tool="nerve_key", args={"key": "D"}, t_session_s=29.0),
        Step(tool="nerve_key", args={"key": "E"}),
    ]
    clock = FakeClock()
    replay.replay(FakeController(), "S", steps, secrets={}, clock=clock, sleep=clock.sleep)
    assert clock.sleeps == [1.0, 3.0]


def test_runner_long_run_diverges_on_its_budget_with_no_dispatch_after_it() -> None:
    steps = [Step(tool="nerve_key", args={"key": str(i)}, t_session_s=3.0 * i) for i in range(40)]
    clock = FakeClock()
    start = clock()
    dispatched_at: list[float] = []

    def dispatch(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        dispatched_at.append(clock())
        return {"success": True}

    result = replay.replay(
        FakeController(), "S", steps, secrets={}, clock=clock, sleep=clock.sleep, dispatch=dispatch
    )
    assert replay.MAX_RUN_S == 75.0
    assert result["outcome"] == "diverged"
    assert result["reason"] == "replay exceeded its 75 s budget"
    assert result["diverged_at"] == 25
    assert result["steps_done"] == 25
    assert len(dispatched_at) == 25
    assert max(dispatched_at) < start + replay.MAX_RUN_S
    assert clock() < start + replay.MAX_RUN_S


def test_runner_never_matching_locator_diverges_at_the_run_budget() -> None:
    ctrl = FakeController(screens=[[]])
    result = _run(ctrl, [_tap(NEXT_TARGET)], step_timeout_s=120.0)
    assert result["outcome"] == "diverged"
    assert result["diverged_at"] == 0
    assert "replay exceeded its 75 s budget" in (result["reason"] or "")
    assert result["duration_s"] == pytest.approx(75.0)
    assert ctrl.calls == []


def test_runner_budget_expiry_skips_the_content_desc_coordinate_fallback() -> None:
    ctrl = FakeController(screens=[[]])
    step = _tap({"content_desc": "Close"})
    result = _run(ctrl, [step], step_timeout_s=10.0, run_budget_s=4.0)
    assert result["outcome"] == "diverged"
    assert result["reason"] == "replay exceeded its 4 s budget waiting for content_desc='Close'"
    assert ctrl.calls == []


def test_runner_element_read_overrunning_the_budget_does_not_tap() -> None:
    clock = FakeClock()

    class SlowReadController(FakeController):
        def elements(self, serial: str, max_depth: int = 30, force_refresh: bool = False) -> list:
            clock.now += 10.0  # a slow uiautomator dump straddles the deadline
            return [NEXT]

    ctrl = SlowReadController()
    result = replay.replay(
        ctrl, "S", [_tap(NEXT_TARGET)], run_budget_s=5.0, clock=clock, sleep=clock.sleep
    )
    assert result["outcome"] == "diverged"
    assert result["reason"] == "replay exceeded its 5 s budget"
    assert ctrl.calls == []


def test_runner_custom_budget_is_reported_in_the_reason() -> None:
    steps = [Step(tool="nerve_key", args={"key": "A"}, t_session_s=3.0 * i) for i in range(3)]
    result = _run(FakeController(), steps, run_budget_s=5.0)
    assert result["reason"] == "replay exceeded its 5 s budget"
    assert result["steps_done"] == 2


def test_runner_unsupported_tool_diverges() -> None:
    result = _run(FakeController(), [Step(tool="nerve_install", args={"path": "a.apk"})])
    assert result["outcome"] == "diverged"
    assert "nerve_install" in (result["reason"] or "")


def test_runner_dispatch_error_dict_diverges_mid_run() -> None:
    def dispatch(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"error": "nope"} if tool == "nerve_key" else {"success": True}

    steps = [Step(tool="nerve_type", args={"text": "a"}), Step(tool="nerve_key", args={})]
    result = _run(FakeController(), steps, dispatch=dispatch)
    assert result["outcome"] == "diverged"
    assert result["diverged_at"] == 1
    assert result["steps_done"] == 1
    assert result["reason"] == "nerve_key failed: nope"


def test_runner_exception_scrubs_secret_from_reason() -> None:
    steps = plan_steps(FIXTURE_LOG)[1:2]
    ctrl = FakeController()
    ctrl.type_error = RuntimeError("could not type hunter2")
    result = _run(ctrl, steps, secrets={"4": "hunter2"})
    assert result["outcome"] == "diverged"
    assert "hunter2" not in json.dumps(result)


# ---------------------------------------------------------------------------
# secrets_env parsing
# ---------------------------------------------------------------------------


def test_parse_secrets_env_resolves_from_environment() -> None:
    environ = {"NERVE_SECRET_PW": "pw", "OTHER": "x"}
    present, missing = "NERVE_SECRET_PW", "NERVE_SECRET_MISSING"
    spec = f"4={present}, 7={missing}"
    assert replay.parse_secrets_env(spec, environ) == {"4": "pw"}
    assert replay.parse_secrets_env("", environ) == {}


@pytest.mark.parametrize(
    "spec", ["4", "x=NERVE_SECRET_PW", "4=", "=NERVE_SECRET_PW", "4=PW", "4=nerve_secret_pw"]
)
def test_parse_secrets_env_rejects_malformed_pairs(spec: str) -> None:
    with pytest.raises(ValueError, match="secrets_env"):
        replay.parse_secrets_env(spec, {})


# ---------------------------------------------------------------------------
# _nerve_replay_sync
# ---------------------------------------------------------------------------


@pytest.fixture
def tool_ctrl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    fake = FakeController(screens=[[NEXT]])
    clock = FakeClock()
    action_log.REGISTRY.reset()
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(tmp_path))
    with (
        patch.object(_core, "_resolve_device", return_value=(object(), "android")),
        patch.object(_core, "_get_controller", return_value=fake),
        patch.object(_core, "_check_guard", return_value=None),
        patch.object(replay, "_clock", clock),
        patch.object(replay, "_sleep", clock.sleep),
    ):
        yield fake
    action_log.REGISTRY.reset()


def _write_log(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def test_replay_tool_missing_file_is_validation_error(tool_ctrl: Any, tmp_path: Path) -> None:
    result = _nerve_replay_sync("S", str(tmp_path / "missing.jsonl"))
    assert result["validation_error"] is True
    assert "not found" in result["error"]


@pytest.mark.parametrize("timeout", [0, -1, math.nan, math.inf, -math.inf, 121])
def test_replay_tool_rejects_bad_inputs(tool_ctrl: Any, tmp_path: Path, timeout: float) -> None:
    log_path = _write_log(tmp_path / "run.jsonl", FIXTURE_LOG)
    bad_json = tmp_path / "bad.jsonl"
    bad_json.write_text('{"type": "header"}\nnot json\n', encoding="utf-8")
    cases = [
        _nerve_replay_sync("S", str(tmp_path / "run.txt")),
        _nerve_replay_sync("S", str(bad_json)),
        _nerve_replay_sync("S", str(log_path), step_timeout_s=timeout),
        _nerve_replay_sync("S", str(log_path), secrets_env="4"),
    ]
    assert all(case["validation_error"] is True for case in cases)
    assert "line 2" in cases[1]["error"]
    assert tool_ctrl.calls == []


def test_replay_tool_runs_log_through_device_tools(tool_ctrl: Any, tmp_path: Path) -> None:
    log_path = _write_log(tmp_path / "run.jsonl", FIXTURE_LOG)
    with patch.object(
        actionlog_tools,
        "_environ",
        {"NERVE_ACTIONLOG_ROOTS": str(tmp_path), "NERVE_SECRET_PW": "pw"},
    ):
        result = _nerve_replay_sync("S", str(log_path), secrets_env="4=NERVE_SECRET_PW")
    assert result["outcome"] == "completed"
    assert result["steps_total"] == 3
    assert tool_ctrl.calls == [
        ("tap", (200, 150)),
        ("type_text", ("pw",)),
        ("press_key", ("BACK",)),
    ]
    assert "pw" not in json.dumps(result)


def test_replay_tool_missing_secret_env_diverges(tool_ctrl: Any, tmp_path: Path) -> None:
    log_path = _write_log(tmp_path / "run.jsonl", FIXTURE_LOG)
    with patch.object(actionlog_tools, "_environ", {"NERVE_ACTIONLOG_ROOTS": str(tmp_path)}):
        result = _nerve_replay_sync("S", str(log_path), secrets_env="4=NERVE_SECRET_PW")
    assert result["outcome"] == "diverged"
    assert result["diverged_at"] == 1
    assert "missing secret" in result["reason"]


def test_replay_tool_unknown_device(tool_ctrl: Any, tmp_path: Path) -> None:
    log_path = _write_log(tmp_path / "run.jsonl", FIXTURE_LOG)
    with patch.object(_core, "_resolve_device", return_value=(None, "Device 'S' not found")):
        assert _nerve_replay_sync("S", str(log_path)) == {"error": "Device 'S' not found"}


def test_replay_tool_respects_reservation_guard(tool_ctrl: Any, tmp_path: Path) -> None:
    log_path = _write_log(tmp_path / "run.jsonl", FIXTURE_LOG)
    with patch.object(_core, "_check_guard", return_value={"error": "reserved by bob"}):
        assert _nerve_replay_sync("S", str(log_path)) == {"error": "reserved by bob"}
    assert tool_ctrl.calls == []


def test_replay_tool_dispatches_through_wrapped_device_tools(
    tool_ctrl: Any, tmp_path: Path
) -> None:
    """A wrapper-level refusal (the #202 guard path) diverges the replay at that step."""
    log_path = _write_log(tmp_path / "run.jsonl", FIXTURE_LOG)
    refusal = {"error": "guard: outside target app", "validation_error": True}
    with (
        patch.object(
            actionlog_tools,
            "_environ",
            {"NERVE_ACTIONLOG_ROOTS": str(tmp_path), "NERVE_SECRET_PW": "pw"},
        ),
        patch.object(input_tools, "_nerve_key_sync", return_value=refusal),
    ):
        result = _nerve_replay_sync("S", str(log_path), secrets_env="4=NERVE_SECRET_PW")
    assert result["outcome"] == "diverged"
    assert result["diverged_at"] == 2
    assert result["reason"] == "nerve_key failed: guard: outside target app"


BUY = {"class": "android.widget.Button", "text": "Buy", "bounds": "[100,100][300,200]"}


class ForegroundController(FakeController):
    """A FakeController whose foreground is the guarded target app."""

    def get_foreground(self, serial: str) -> dict[str, str]:
        return {"package": "com.example.app", "activity": ".Main"}


def test_guarded_replay_of_a_tap_on_buy_diverges_with_denied_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replayed tap on a deny-labelled element is refused by the #202 guard.

    No dispatch seam is patched: the step goes through the real _tool_dispatch
    into the wrapped nerve_tap, whose guard refuses it before the device tap.
    """
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(tmp_path))
    fake = ForegroundController(screens=[[BUY]])
    clock = FakeClock()
    buy_target = {**NEXT_TARGET, "text": "Buy"}
    source = _write_log(
        tmp_path / "run.jsonl", [FIXTURE_LOG[0], {**FIXTURE_LOG[2], "target": buy_target}]
    )
    guarded_log = tmp_path / "guarded.jsonl"
    action_log.REGISTRY.reset()
    with (
        patch.object(_core, "_resolve_device", return_value=(object(), "android")),
        patch.object(_core, "_get_controller", return_value=fake),
        patch.object(_core, "_check_guard", return_value=None),
        patch.object(replay, "_clock", clock),
        patch.object(replay, "_sleep", clock.sleep),
    ):
        started = _nerve_actionlog_start_sync(
            "S", str(guarded_log), target_package="com.example.app"
        )
        assert started["guarded"] is True
        result = _nerve_replay_sync("S", str(source))
        stopped = _nerve_actionlog_stop_sync("S")
    action_log.REGISTRY.reset()

    assert result["outcome"] == "diverged"
    assert result["diverged_at"] == 0
    assert result["reason"] == "nerve_tap failed: guarded session: input on a 'buy' label refused"
    assert fake.calls == []
    assert stopped["guard_refusals"] == 1
    lines = [json.loads(line) for line in guarded_log.read_text(encoding="utf-8").splitlines()]
    guard_lines = [
        (line["tool"], line["reason"], line["detail"]) for line in lines if line["type"] == "guard"
    ]
    assert guard_lines == [("nerve_tap", "denied_label", "buy")]


def test_replay_while_logging_logs_steps_not_the_replay(tool_ctrl: Any, tmp_path: Path) -> None:
    source = _write_log(tmp_path / "run.jsonl", FIXTURE_LOG)
    new_log = tmp_path / "replayed.jsonl"
    _nerve_actionlog_start_sync("S", str(new_log))
    with patch.object(
        actionlog_tools,
        "_environ",
        {"NERVE_ACTIONLOG_ROOTS": str(tmp_path), "NERVE_SECRET_PW": "secretpw"},
    ):
        result = _nerve_replay_sync("S", str(source), secrets_env="4=NERVE_SECRET_PW")
    _nerve_actionlog_stop_sync("S")

    assert result["outcome"] == "completed"
    text = new_log.read_text(encoding="utf-8")
    assert "secretpw" not in text
    tools = [json.loads(line).get("tool") for line in text.splitlines()[1:]]
    assert tools == ["nerve_tap", "nerve_type", "nerve_key"]
    assert "nerve_replay" in action_log.UNLOGGED_TOOLS
    assert [s.tool for s in plan_steps([json.loads(x) for x in text.splitlines()])] == tools


def test_replay_tool_is_registered_device_tool() -> None:
    assert _core.TOOL_REGISTRY["nerve_replay"]["kind"] == "device"
    doc = _nerve_replay_sync.__doc__ or ""
    assert "USE FOR" in doc and "NOT FOR" in doc and "TIP" in doc


def test_replay_tool_edge_inputs(tool_ctrl: Any, tmp_path: Path) -> None:
    unreadable = tmp_path / "dir.jsonl"
    unreadable.mkdir()
    assert "Cannot read" in _nerve_replay_sync("S", str(unreadable))["error"]

    entries = [{"type": "action", "seq": 1, "tool": "nerve_bogus", "args": {}, "ok": True}]
    log_path = _write_log(tmp_path / "bogus.jsonl", entries)
    log_path.write_text("\n" + log_path.read_text(encoding="utf-8"), encoding="utf-8")
    result = _nerve_replay_sync("S", str(log_path))
    assert result["outcome"] == "completed"
    assert result["steps_total"] == 0
    assert tool_ctrl.calls == []


def test_replay_tool_accepts_step_timeout_at_the_cap(tool_ctrl: Any, tmp_path: Path) -> None:
    log_path = _write_log(tmp_path / "run.jsonl", FIXTURE_LOG[:3])
    result = _nerve_replay_sync("S", str(log_path), step_timeout_s=120)
    assert result["outcome"] == "completed"


# ---------------------------------------------------------------------------
# Input hardening (desk review of 590f222)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "var", ["GH_TOKEN", "PATH", "NERVE_PW", "NERVE_SECRET_", "nerve_secret_x"]
)
def test_replay_tool_rejects_non_secret_env_vars(tool_ctrl: Any, tmp_path: Path, var: str) -> None:
    """Only NERVE_SECRET_* variables can be typed — no daemon env exfiltration."""
    log_path = _write_log(tmp_path / "run.jsonl", FIXTURE_LOG)
    environ = {
        "NERVE_ACTIONLOG_ROOTS": str(tmp_path),
        "GH_TOKEN": "ghp_x",
        "PATH": "/bin",
        "NERVE_PW": "pw",
        "nerve_secret_x": "x",
    }
    with patch.object(actionlog_tools, "_environ", environ):
        result = _nerve_replay_sync("S", str(log_path), secrets_env=f"4={var}")
    assert result["validation_error"] is True
    assert "NERVE_SECRET_" in result["error"]
    assert tool_ctrl.calls == []
    assert tool_ctrl.element_kwargs == []


def _action(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "action",
        "seq": 1,
        "tool": "nerve_key",
        "args": {"key": "BACK"},
        "duration_ms": 30.0,
        "ok": True,
        "error": None,
        "t_session_s": 0.0,
    }
    entry.update(overrides)
    return entry


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("args", "xy"),
        ("args", 5),
        ("args", ["x"]),
        ("seq", "1"),
        ("seq", 1.5),
        ("seq", True),
        ("duration_ms", "abc"),
        ("duration_ms", True),
        ("duration_ms", [1]),
        ("t_session_s", "x"),
        ("t_session_s", False),
        ("t_session_s", {"a": 1}),
    ],
)
def test_replay_tool_malformed_line_is_validation_error(
    tool_ctrl: Any, tmp_path: Path, field: str, value: Any
) -> None:
    entries = [FIXTURE_LOG[0], _action(seq=1), _action(**{"seq": 2, field: value})]
    log_path = _write_log(tmp_path / "run.jsonl", entries)
    log_path.write_text("\n" + log_path.read_text(encoding="utf-8"), encoding="utf-8")
    result = _nerve_replay_sync("S", str(log_path))
    assert result["validation_error"] is True
    assert "line 4" in result["error"]
    assert field in result["error"]
    assert tool_ctrl.calls == []


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(
            '{"type": "action", "tool": "nerve_key", "ok": true, "args": {"key": "A"}, '
            '"t_session_s": NaN}',
            id="nan-t_session_s",
        ),
        pytest.param("[" * 50_000, id="deeply-nested"),
    ],
)
def test_replay_tool_non_finite_or_deep_line_is_validation_error(
    tool_ctrl: Any, tmp_path: Path, line: str
) -> None:
    log_path = tmp_path / "run.jsonl"
    log_path.write_text(line + "\n", encoding="utf-8")
    result = _nerve_replay_sync("S", str(log_path))
    assert result["validation_error"] is True
    assert "line 1" in result["error"]
    assert tool_ctrl.calls == []


def test_replay_tool_non_utf8_log_is_validation_error(tool_ctrl: Any, tmp_path: Path) -> None:
    log_path = tmp_path / "run.jsonl"
    log_path.write_bytes(b"\xff\xfe not utf-8\n")
    result = _nerve_replay_sync("S", str(log_path))
    assert result["validation_error"] is True
    assert "UTF-8" in result["error"]


def test_plan_steps_null_optional_fields_are_fine() -> None:
    entry = _action(seq=None, duration_ms=None, t_session_s=None, args={"key": "A"})
    (step,) = plan_steps([entry])
    assert step.seq is None and step.t_session_s is None and step.duration_s == 0.0


def test_plan_steps_malformed_line_raises_with_its_line_number() -> None:
    with pytest.raises(ValueError, match=r"line 2: args"):
        plan_steps([_action(), _action(args="xy")])


def test_replay_tool_rejects_oversized_log(tool_ctrl: Any, tmp_path: Path) -> None:
    log_path = _write_log(tmp_path / "run.jsonl", FIXTURE_LOG)
    size = log_path.stat().st_size
    with patch.object(replay, "MAX_LOG_BYTES", size - 1):
        result = _nerve_replay_sync("S", str(log_path))
    assert result["validation_error"] is True
    assert "MiB" in result["error"] or "bytes" in result["error"]
    assert tool_ctrl.calls == []
    with patch.object(replay, "MAX_LOG_BYTES", size):
        result = _nerve_replay_sync("S", str(log_path))
    assert "validation_error" not in result
    assert result["steps_total"] == 3


def test_replay_tool_rejects_too_many_steps(tool_ctrl: Any, tmp_path: Path) -> None:
    log_path = _write_log(tmp_path / "run.jsonl", FIXTURE_LOG)  # 3 planned steps
    with patch.object(replay, "MAX_STEPS", 2):
        result = _nerve_replay_sync("S", str(log_path))
    assert result["validation_error"] is True
    assert "steps" in result["error"]
    assert tool_ctrl.calls == []


def test_replay_tool_rejects_non_regular_file(tool_ctrl: Any, tmp_path: Path) -> None:
    fifo = tmp_path / "pipe.jsonl"
    os.mkfifo(fifo)
    result = _nerve_replay_sync("S", str(fifo))
    assert result["validation_error"] is True
    assert "regular file" in result["error"]


@pytest.mark.parametrize(
    "tool",
    [
        "nerve_uninstall",
        "nerve_clear_app_data",
        "nerve_install",
        "nerve_emulator_stop",
        "nerve_reserve",
        "nerve_release",
        "nerve_grant_permission",
        "nerve_device_state",
    ],
)
def test_plan_steps_only_keeps_the_replayable_input_tools(tool: str) -> None:
    entries = [_action(tool=tool, args={"package": "com.x"}), _action(seq=2)]
    assert [s.tool for s in plan_steps(entries)] == ["nerve_key"]


def test_replay_tool_never_dispatches_uninstall(tool_ctrl: Any, tmp_path: Path) -> None:
    import linc_nerve.mcp.app_tools as app_tools

    entries = [FIXTURE_LOG[0], _action(tool="nerve_uninstall", args={"package": "com.x"})]
    log_path = _write_log(tmp_path / "run.jsonl", entries)
    with patch.object(app_tools, "_nerve_uninstall_sync") as uninstall:
        result = _nerve_replay_sync("S", str(log_path))
    uninstall.assert_not_called()
    assert result["steps_total"] == 0


def test_tool_dispatch_refuses_non_replayable_tool() -> None:
    import linc_nerve.mcp.app_tools as app_tools

    with patch.object(app_tools, "_nerve_uninstall_sync") as uninstall:
        out = actionlog_tools._tool_dispatch("S")("nerve_uninstall", {"package": "com.x"})
    uninstall.assert_not_called()
    assert out == {"error": "nerve_uninstall is not replayable"}


@pytest.mark.parametrize(
    "args",
    [
        {"text": "<redacted>", "secret": "true"},
        {"text": "<redacted>"},
        {"text": "<redacted>", "secret": False},
        {"text": "hunter2", "secret": 1},
        {"text": "hunter2", "secret": "yes"},
    ],
)
def test_runner_redacted_or_truthy_secret_type_takes_secret_path(args: dict[str, Any]) -> None:
    (step,) = plan_steps([_action(seq=7, tool="nerve_type", args=args)])
    ctrl = FakeController()
    result = _run(ctrl, [step], secrets={})
    assert result["outcome"] == "diverged"
    assert result["reason"] == "missing secret for step seq 7"
    assert ctrl.calls == []
    ctrl = FakeController()
    assert _run(ctrl, [step], secrets={"7": "pw"})["outcome"] == "completed"
    assert ctrl.calls == [("type_text", ("pw",))]


def test_replay_tool_redacted_string_secret_flag_is_not_typed(
    tool_ctrl: Any, tmp_path: Path
) -> None:
    entries = [
        FIXTURE_LOG[0],
        _action(tool="nerve_type", args={"text": "<redacted>", "secret": "true"}),
    ]
    log_path = _write_log(tmp_path / "run.jsonl", entries)
    result = _nerve_replay_sync("S", str(log_path))
    assert result["outcome"] == "diverged"
    assert "missing secret" in result["reason"]
    assert tool_ctrl.calls == []


def test_replay_tool_read_failures_are_validation_errors(tool_ctrl: Any, tmp_path: Path) -> None:
    log_path = _write_log(tmp_path / "run.jsonl", FIXTURE_LOG)
    with patch.object(actionlog_tools.os, "open", side_effect=PermissionError("denied")):
        assert "Cannot read action log: denied" in _nerve_replay_sync("S", str(log_path))["error"]
    with patch.object(actionlog_tools, "_read_regular", side_effect=OSError("EIO")):
        assert "Cannot read action log: EIO" in _nerve_replay_sync("S", str(log_path))["error"]
    grown = b"x" * (replay.MAX_LOG_BYTES + 1)
    with patch.object(actionlog_tools, "_read_regular", return_value=grown):
        result = _nerve_replay_sync("S", str(log_path))
    assert result["validation_error"] is True
    assert "larger than" in result["error"]
    assert tool_ctrl.calls == []


def test_registered_device_tool_ignores_global_and_unknown_tools() -> None:
    assert actionlog_tools._registered_device_tool("nerve_discover") is None
    assert actionlog_tools._registered_device_tool("nerve_nope") is None
    assert actionlog_tools._registered_device_tool("nerve_key") is not None


def test_element_centre_none_for_unparseable_bounds() -> None:
    assert replay.element_centre({"bounds": "junk"}) is None
    assert replay.element_centre({"bounds": "[0,0][10,20]"}) == (5, 10)
