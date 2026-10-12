"""Guarded-session bypasses found on desk re-review (linc-nerve-2wh, round 3).

Each test pins one way an agent could get an activation past the guard of a
guarded action-log session: an empty or truncated element tree, a
zero-length swipe (a click on Android), an activation key on a focused
deny-labelled element, and label spellings the matcher did not fold. The fake
controller from ``test_guarded_session`` stands in for adb/WDA; no test spawns
adb or talks to :8111.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from linc_nerve.core import action_log
from linc_nerve.core.action_log import denied_label
from linc_nerve.core.android import AndroidController
from linc_nerve.mcp import _core, input_tools
from tests.test_guarded_session import (
    TARGET,
    FakeController,
    _lines,
    _start,
    _tool_calls,
)


@pytest.fixture(name="ctrl")
def _ctrl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FakeController]:
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


# ---------------------------------------------------------------------------
# 1. Empty tree — a loading screen is not "nothing under the point"
# ---------------------------------------------------------------------------


def test_tap_refused_on_empty_element_tree(ctrl: FakeController, tmp_path: Path) -> None:
    path = _start(tmp_path, target_package=TARGET)
    ctrl.tree = []

    result = input_tools._nerve_tap_sync("S", 150, 150)

    assert result["guard"] == "target_unreadable"
    assert result["detail"] == "empty element tree"
    assert _tool_calls(ctrl, "tap") == []
    (line,) = _lines(path, "guard")
    assert line["detail"] == "empty element tree"


def test_unguarded_tap_on_empty_tree_still_runs(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path)
    ctrl.tree = []

    assert input_tools._nerve_tap_sync("S", 150, 150)["success"] is True


# ---------------------------------------------------------------------------
# 2. Truncated tree — the 80-node cap must not hide a "Buy" button
# ---------------------------------------------------------------------------

_FILLER = [{"text": f"row {i}", "bounds": "[0,0][10,10]"} for i in range(80)]
_BUY_AT_81 = [*_FILLER, {"text": "Buy", "bounds": "[100,100][300,200]"}]


class CappingController(FakeController):
    """An Android-like elements() honouring max_nodes / force_refresh."""

    def __init__(self) -> None:
        super().__init__()
        self.element_kwargs: list[dict[str, Any]] = []

    def elements(  # type: ignore[override]
        self,
        serial: str,
        max_depth: int = 30,
        force_refresh: bool = False,
        max_nodes: int | None = 80,
    ) -> list[dict[str, Any]]:
        self.element_kwargs.append(
            {"max_depth": max_depth, "force_refresh": force_refresh, "max_nodes": max_nodes}
        )
        return _BUY_AT_81 if max_nodes is None else _BUY_AT_81[:max_nodes]


@pytest.fixture
def capping(ctrl: FakeController) -> CappingController:
    fake = CappingController()
    _core._get_controller.return_value = fake  # type: ignore[attr-defined]
    return fake


def test_buy_past_node_80_is_refused(capping: CappingController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)

    result = input_tools._nerve_tap_sync("S", 150, 150)

    assert result["guard"] == "denied_label"
    assert result["detail"] == "buy"
    assert _tool_calls(capping, "tap") == []


def test_guard_reads_an_uncapped_fresh_tree(capping: CappingController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)

    input_tools._nerve_tap_sync("S", 150, 150)

    (kwargs,) = capping.element_kwargs
    assert kwargs["max_nodes"] is None
    assert kwargs["force_refresh"] is True
    assert kwargs["max_depth"] == action_log.GUARD_MAX_DEPTH


def test_unguarded_session_keeps_the_capped_cached_read(
    capping: CappingController, tmp_path: Path
) -> None:
    _start(tmp_path)

    assert input_tools._nerve_tap_sync("S", 150, 150)["success"] is True
    assert capping.element_kwargs == [{"max_depth": 30, "force_refresh": False, "max_nodes": 80}]


def test_capped_reader_at_80_nodes_is_refused(ctrl: FakeController, tmp_path: Path) -> None:
    """A reader without max_nodes (iOS) that returns a full 80 may be truncated."""
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = _BUY_AT_81[:80]

    result = input_tools._nerve_tap_sync("S", 150, 150)

    assert result["guard"] == "target_unreadable"
    assert "truncated" in result["detail"]
    assert _tool_calls(ctrl, "tap") == []


def _hierarchy_xml(count: int) -> str:
    nodes = "".join(f'<node text="n{i}" bounds="[0,0][1,1]" />' for i in range(count))
    return f'<hierarchy rotation="0"><node class="root">{nodes}</node></hierarchy>'


def _android_with(xml: str) -> AndroidController:
    android = AndroidController()
    device = MagicMock()
    device.shell.return_value = xml
    client = MagicMock()
    client.device.return_value = device
    android._client = client
    return android


class TestAndroidMaxNodes:
    def test_default_cap_is_80(self) -> None:
        assert len(_android_with(_hierarchy_xml(120)).elements("ABC")) == 80

    def test_max_nodes_none_is_uncapped(self) -> None:
        nodes = _android_with(_hierarchy_xml(120)).elements("ABC", max_nodes=None)
        assert len(nodes) == 120

    def test_uncapped_read_does_not_feed_the_capped_cache(self) -> None:
        android = _android_with(_hierarchy_xml(120))
        android.elements("ABC", max_nodes=None, force_refresh=True)
        assert len(android.elements("ABC")) == 80


# ---------------------------------------------------------------------------
# 3. A zero-length swipe is a tap
# ---------------------------------------------------------------------------


def test_zero_length_swipe_on_buy_refused(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.label = {"text": "Buy"}

    result = input_tools._nerve_swipe_sync("S", 150, 150, 150, 150)

    assert result["guard"] == "denied_label"
    assert result["detail"] == "buy"
    assert _tool_calls(ctrl, "swipe") == []


def test_short_swipe_on_buy_refused(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.label = {"text": "Buy"}

    result = input_tools._nerve_swipe_sync("S", 150, 150, 180, 180)  # ~42 px

    assert result["guard"] == "denied_label"


def test_real_swipe_from_buy_executes(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.label = {"text": "Buy"}

    assert input_tools._nerve_swipe_sync("S", 150, 150, 150, 900)["success"] is True
    assert len(_tool_calls(ctrl, "swipe")) == 1


def test_unguarded_short_swipe_reads_no_elements(ctrl: FakeController, tmp_path: Path) -> None:
    path = _start(tmp_path)
    ctrl.elements_error = AssertionError("unguarded swipe must not read the tree")

    assert input_tools._nerve_swipe_sync("S", 150, 150, 150, 150)["success"] is True
    (action,) = _lines(path, "action")
    assert "target" not in action


class TestTouchPoint:
    def test_tap_point(self) -> None:
        assert action_log.touch_point("nerve_tap", {"x": 1, "y": 2}) == (1, 2)

    def test_short_swipe_is_its_start(self) -> None:
        args = {"start_x": 10, "start_y": 20, "end_x": 10, "end_y": 68}
        assert action_log.touch_point("nerve_swipe", args) == (10, 20)

    def test_long_swipe_has_no_point(self) -> None:
        args = {"start_x": 10, "start_y": 20, "end_x": 10, "end_y": 69}
        assert action_log.touch_point("nerve_swipe", args) is None

    def test_other_tools_have_no_point(self) -> None:
        assert action_log.touch_point("nerve_type", {"text": "x"}) is None


# ---------------------------------------------------------------------------
# 4. Activation keys on a focused element
# ---------------------------------------------------------------------------

_FOCUSED_SUBSCRIBE = [
    {"class": "android.widget.FrameLayout", "bounds": "[0,0][1080,2400]"},
    {"class": "android.widget.Button", "bounds": "[100,100][500,300]", "focused": True},
    {"class": "android.widget.TextView", "bounds": "[150,150][400,250]", "text": "Subscribe"},
]


@pytest.mark.parametrize(
    "key",
    [
        *("ENTER", "KEYCODE_ENTER", "dpad_center", "KEYCODE_DPAD_CENTER", "NUMPAD_ENTER"),
        *("SPACE", "66", "23", "160", "62"),
    ],
)
def test_activation_key_on_focused_deny_label_refused(
    ctrl: FakeController,
    tmp_path: Path,
    key: str,
) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = _FOCUSED_SUBSCRIBE

    result = input_tools._nerve_key_sync("S", key)

    assert result["guard"] == "denied_label"
    assert result["detail"] == "subscribe"
    assert _tool_calls(ctrl, "press_key") == []


def test_activation_key_with_harmless_focus_executes(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = [{**_FOCUSED_SUBSCRIBE[1]}, {**_FOCUSED_SUBSCRIBE[2], "text": "Next"}]

    assert input_tools._nerve_key_sync("S", "ENTER")["success"] is True


def test_activation_key_unreadable_tree_refused(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.elements_error = RuntimeError("uiautomator dump killed")

    result = input_tools._nerve_key_sync("S", "ENTER")

    assert result["guard"] == "target_unreadable"
    assert _tool_calls(ctrl, "press_key") == []


def test_non_activation_key_reads_no_elements(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.elements_error = AssertionError("VOLUME_UP must not read the tree")

    assert input_tools._nerve_key_sync("S", "VOLUME_UP")["success"] is True


@pytest.mark.parametrize("key", ["KEYCODE_3", "KEYCODE_4"])
def test_digit_keycodes_are_not_escape_keys(
    ctrl: FakeController,
    tmp_path: Path,
    key: str,
) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.foreground = "com.android.vending"

    result = input_tools._nerve_key_sync("S", key)

    assert result["guard"] == "foreground_not_allowed"
    assert _tool_calls(ctrl, "press_key") == []


class TestKeyClassifiers:
    @pytest.mark.parametrize("key", ["3", "4", "BACK", "home", "KEYCODE_BACK"])
    def test_escape(self, key: str) -> None:
        assert action_log.is_escape_key(key) is True

    @pytest.mark.parametrize("key", ["KEYCODE_3", "KEYCODE_4", "ENTER", None])
    def test_not_escape(self, key: Any) -> None:
        assert action_log.is_escape_key(key) is False

    @pytest.mark.parametrize("key", ["VOLUME_UP", "TAB", None])
    def test_not_activation(self, key: Any) -> None:
        assert action_log.is_activation_key(key) is False

    def test_focus_labels_cover_the_focused_element_and_its_content(self) -> None:
        labels = action_log.focus_labels(_FOCUSED_SUBSCRIBE)
        assert [label["text"] for label in labels] == ["", "Subscribe"]

    def test_no_focus_no_labels(self) -> None:
        assert action_log.focus_labels(_FOCUSED_SUBSCRIBE[2:]) == []


# ---------------------------------------------------------------------------
# 5. Matcher folding and canonical phrases
# ---------------------------------------------------------------------------


class TestMatcher:
    @pytest.mark.parametrize(
        ("label", "phrase"),
        [
            ("Delete​account", "delete account"),
            ("Delete-account", "delete account"),
            ("Delete_account", "delete account"),
            ("B͏uy", "buy"),
            ("Paÿ", "pay"),
            ("Bㅤuy", "buy"),
            ("Bᅟuy", "buy"),
            ("Bᅠuy", "buy"),
            ("Bﾠuy", "buy"),
            ("Subscrİbe", "subscribe"),
        ],
    )
    def test_obfuscated_labels_match(self, label: str, phrase: str) -> None:
        assert denied_label(label) == phrase

    def test_word_boundary_still_holds(self) -> None:
        assert denied_label("Unsubscribe") is None

    def test_returns_the_canonical_custom_phrase(self) -> None:
        pattern = action_log.deny_pattern(["Launch  Missiles"])
        assert denied_label("LAUNCH-missiles now", pattern) == "launch missiles"
