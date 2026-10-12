"""Guarded action-log session (linc-nerve-2wh).

While an action log names a ``target_package`` the session is GUARDED: input
tools refuse when the foreground app is outside the allowed packages, taps on
deny-listed labels are refused, and app-management tools refuse any package
other than the target. A fake controller stands in for adb/WDA
(``patch.object(_core, ...)``); the log lives under ``tmp_path``. No test
spawns adb or talks to :8111.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from linc_nerve.core import action_log
from linc_nerve.core.action_log import denied_label
from linc_nerve.mcp import _core, app_tools, input_tools
from linc_nerve.mcp.actionlog_tools import (
    _nerve_actionlog_start_sync,
    _nerve_actionlog_stop_sync,
)

TARGET = "com.example.app"


class FakeController:
    """Records every device call; foreground + element tree are settable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.foreground = TARGET
        self.foreground_calls = 0
        self.foreground_error: Exception | None = None
        self.label: dict[str, str] = {"text": "Next"}
        self.elements_error: Exception | None = None
        self.tree: list[dict[str, Any]] | None = None

    def get_foreground(self, serial: str) -> dict[str, str]:
        self.foreground_calls += 1
        if self.foreground_error is not None:
            raise self.foreground_error
        return {"package": self.foreground, "activity": ".Main"}

    def elements(self, serial: str) -> list[dict[str, Any]]:
        if self.elements_error is not None:
            raise self.elements_error
        if self.tree is not None:
            return self.tree
        return [
            {"class": "android.widget.FrameLayout", "bounds": "[0,0][1080,2400]"},
            {"class": "android.widget.Button", "bounds": "[100,100][300,200]", **self.label},
        ]

    def tap(self, serial: str, x: int, y: int) -> None:
        self.calls.append(("tap", (serial, x, y)))

    def long_press(self, serial: str, x: int, y: int, duration_ms: int) -> None:
        self.calls.append(("long_press", (serial, x, y, duration_ms)))

    def swipe(self, serial: str, *args: Any) -> None:
        self.calls.append(("swipe", (serial, *args)))

    def type_text(self, serial: str, text: str) -> None:
        self.calls.append(("type_text", (serial, text)))

    def press_key(self, serial: str, key: str) -> None:
        self.calls.append(("press_key", (serial, key)))

    def launch(self, serial: str, package: str, **kwargs: Any) -> None:
        self.calls.append(("launch", (serial, package)))

    def uninstall(self, serial: str, package: str) -> None:
        self.calls.append(("uninstall", (serial, package)))

    def clear_app_data(self, serial: str, package: str) -> None:
        self.calls.append(("clear_app_data", (serial, package)))

    def grant_permission(self, serial: str, package: str, permission: str) -> None:
        self.calls.append(("grant_permission", (serial, package, permission)))


class NoForegroundController(FakeController):
    """A platform without a foreground API (iOS today)."""

    get_foreground = None  # type: ignore[assignment]


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


def _start(tmp_path: Path, **kwargs: Any) -> Path:
    path = tmp_path / "run.jsonl"
    started = _nerve_actionlog_start_sync("S", str(path), **kwargs)
    assert started["success"] is True
    return path


def _lines(path: Path, kind: str) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r for r in records if r["type"] == kind]


def _tool_calls(fake: FakeController, name: str) -> list[tuple[Any, ...]]:
    return [args for call, args in fake.calls if call == name]


# ---------------------------------------------------------------------------
# denied_label — pure
# ---------------------------------------------------------------------------


class TestDeniedLabel:
    def test_matches_a_deny_word(self) -> None:
        assert denied_label("Buy now") == "buy"

    def test_word_boundary(self) -> None:
        assert denied_label("Subscription details") is None
        assert denied_label("Poster gallery") is None

    def test_multi_word_phrase(self) -> None:
        assert denied_label("Delete account") == "delete account"
        assert denied_label("Delete  my\naccount") == "delete my account"

    def test_empty_and_none(self) -> None:
        assert denied_label("") is None
        assert denied_label(None) is None

    def test_case_insensitive(self) -> None:
        assert denied_label("SUBSCRIBE") == "subscribe"
        assert denied_label("Upgrade to Pro") == "upgrade"

    def test_harmless_label(self) -> None:
        assert denied_label("Next") is None

    def test_zero_width_characters_are_stripped(self) -> None:
        assert denied_label("B\u200buy") == "buy"
        assert denied_label("Sub\u00adscribe") == "subscribe"

    def test_fullwidth_forms_are_nfkc_normalised(self) -> None:
        assert denied_label("\uff22\uff35\uff39") == "buy"

    def test_custom_words_replace_the_default(self) -> None:
        pattern = action_log.deny_pattern(["frobnicate"])
        assert denied_label("Buy now", pattern) is None
        assert denied_label("Frobnicate it", pattern) == "frobnicate"


class TestGuardConfig:
    def test_no_target_means_unguarded(self) -> None:
        assert action_log.build_guard("", "com.a", "x") is None

    def test_allowed_packages_union(self) -> None:
        guard = action_log.build_guard(TARGET, " com.a , com.b,", "")
        assert guard is not None
        assert {TARGET, "com.a", "com.b"} <= guard.allowed_packages
        assert guard.allowed_packages >= action_log.BUILTIN_ALLOWED_PACKAGES
        assert "" not in guard.allowed_packages

    def test_builtin_set_covers_system_dialogs(self) -> None:
        assert "android" in action_log.BUILTIN_ALLOWED_PACKAGES
        assert "com.google.android.permissioncontroller" in action_log.BUILTIN_ALLOWED_PACKAGES


# ---------------------------------------------------------------------------
# Foreground guard on input tools
# ---------------------------------------------------------------------------


def test_tap_refused_when_foreground_not_allowed(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.foreground = "com.android.vending"

    result = input_tools._nerve_tap_sync("S", 150, 150)

    assert result["guard"] == "foreground_not_allowed"
    assert result["validation_error"] is True
    assert result["error"] == (
        "guarded session: foreground com.android.vending is outside the allowed apps"
    )
    assert _tool_calls(ctrl, "tap") == []


@pytest.mark.parametrize(
    ("call", "fake_name"),
    [
        (lambda: input_tools._nerve_long_press_sync("S", 150, 150), "long_press"),
        (lambda: input_tools._nerve_swipe_sync("S", 1, 2, 3, 4), "swipe"),
        (lambda: input_tools._nerve_type_sync("S", "hello"), "type_text"),
        (lambda: input_tools._nerve_key_sync("S", "ENTER"), "press_key"),
    ],
)
def test_other_input_tools_refused_outside_allowed(
    ctrl: FakeController, tmp_path: Path, call: Any, fake_name: str
) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.foreground = "com.android.vending"

    result = call()

    assert result["guard"] == "foreground_not_allowed"
    assert _tool_calls(ctrl, fake_name) == []


@pytest.mark.parametrize("key", ["BACK", "HOME", "back", "KEYCODE_HOME"])
def test_back_and_home_always_allowed(ctrl: FakeController, tmp_path: Path, key: str) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.foreground = "com.android.vending"

    result = input_tools._nerve_key_sync("S", key)

    assert result["success"] is True
    assert _tool_calls(ctrl, "press_key") == [("S", key)]


def test_allow_packages_and_builtin_set_pass(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET, allow_packages="com.example.login")

    for foreground in ("com.example.login", "com.google.android.permissioncontroller"):
        ctrl.foreground = foreground
        assert input_tools._nerve_tap_sync("S", 150, 150)["success"] is True
    assert len(_tool_calls(ctrl, "tap")) == 2


def test_unreadable_foreground_is_refused(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.foreground_error = RuntimeError("adb read timeout")

    result = input_tools._nerve_tap_sync("S", 150, 150)

    assert result["guard"] == "foreground_not_allowed"
    assert "unknown" in result["error"]
    assert _tool_calls(ctrl, "tap") == []


def test_platform_without_foreground_api_skips_that_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = NoForegroundController()
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(tmp_path))
    action_log.REGISTRY.reset()
    with (
        patch.object(_core, "_resolve_device", return_value=(object(), "ios")),
        patch.object(_core, "_get_controller", return_value=fake),
        patch.object(_core, "_check_guard", return_value=None),
    ):
        _start(tmp_path, target_package=TARGET)
        assert input_tools._nerve_tap_sync("S", 150, 150)["success"] is True
        fake.label = {"text": "Buy"}
        assert input_tools._nerve_tap_sync("S", 150, 150)["guard"] == "denied_label"
    action_log.REGISTRY.reset()


# ---------------------------------------------------------------------------
# Deny-listed labels
# ---------------------------------------------------------------------------


def test_tap_on_denied_label_refused(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.label = {"text": "Subscribe"}

    result = input_tools._nerve_tap_sync("S", 150, 150)

    assert result["guard"] == "denied_label"
    assert result["detail"] == "subscribe"
    assert "subscribe" in result["error"]
    assert result["validation_error"] is True
    assert _tool_calls(ctrl, "tap") == []


def test_tap_on_harmless_label_executes(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.label = {"text": "Next"}

    result = input_tools._nerve_tap_sync("S", 150, 150)

    assert result == {"success": True, "action": "tap", "x": 150, "y": 150}
    assert _tool_calls(ctrl, "tap") == [("S", 150, 150)]


def test_long_press_on_content_desc_refused(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.label = {"desc": "Delete account"}

    result = input_tools._nerve_long_press_sync("S", 150, 150)

    assert result["guard"] == "denied_label"
    assert result["detail"] == "delete account"
    assert _tool_calls(ctrl, "long_press") == []


def test_tap_outside_any_labelled_element_executes(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.label = {"text": "Buy"}

    assert input_tools._nerve_tap_sync("S", 900, 900)["success"] is True


def test_custom_deny_words_replace_default(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET, deny_words="launch missiles, nuke")
    ctrl.label = {"text": "Buy"}
    assert input_tools._nerve_tap_sync("S", 150, 150)["success"] is True

    ctrl.label = {"text": "Launch missiles"}
    result = input_tools._nerve_tap_sync("S", 150, 150)
    assert result["guard"] == "denied_label"
    assert result["detail"] == "launch missiles"


# ---------------------------------------------------------------------------
# App-management tools stay on the target
# ---------------------------------------------------------------------------


def test_launch_other_package_refused(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)

    result = app_tools._nerve_launch_sync("S", "com.other.app")

    assert result["guard"] == "other_package"
    assert result["detail"] == "com.other.app"
    assert result["validation_error"] is True
    assert _tool_calls(ctrl, "launch") == []


def test_launch_target_allowed_even_from_another_app(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.foreground = "com.android.vending"

    result = app_tools._nerve_launch_sync("S", TARGET)

    assert result["success"] is True
    assert _tool_calls(ctrl, "launch") == [("S", TARGET)]


@pytest.mark.parametrize(
    ("call", "fake_name"),
    [
        (lambda: app_tools._nerve_uninstall_sync("S", "com.other.app", confirm=True), "uninstall"),
        (
            lambda: app_tools._nerve_clear_app_data_sync("S", "com.other.app", confirm=True),
            "clear_app_data",
        ),
        (
            lambda: app_tools._nerve_grant_permission_sync(
                "S", "com.other.app", "android.permission.CAMERA", confirm=True
            ),
            "grant_permission",
        ),
    ],
)
def test_package_tools_refuse_other_package(
    ctrl: FakeController, tmp_path: Path, call: Any, fake_name: str
) -> None:
    _start(tmp_path, target_package=TARGET)

    result = call()

    assert result["guard"] == "other_package"
    assert _tool_calls(ctrl, fake_name) == []


# ---------------------------------------------------------------------------
# Refusals in the log + stop result
# ---------------------------------------------------------------------------


def test_refusals_logged_and_counted(ctrl: FakeController, tmp_path: Path) -> None:
    path = _start(tmp_path, target_package=TARGET)
    ctrl.label = {"text": "Subscribe"}
    input_tools._nerve_tap_sync("S", 150, 150)
    app_tools._nerve_launch_sync("S", "com.other.app")
    ctrl.foreground = "com.android.vending"
    input_tools._nerve_swipe_sync("S", 1, 2, 3, 4)
    input_tools._nerve_key_sync("S", "BACK")  # allowed — an action line

    stopped = _nerve_actionlog_stop_sync("S")

    guards = _lines(path, "guard")
    assert [(g["tool"], g["reason"], g["detail"]) for g in guards] == [
        ("nerve_tap", "denied_label", "subscribe"),
        ("nerve_launch", "other_package", "com.other.app"),
        ("nerve_swipe", "foreground_not_allowed", "com.android.vending"),
    ]
    assert stopped["guard_refusals"] == 3
    assert stopped["entries"] == 1
    assert [a["tool"] for a in _lines(path, "action")] == ["nerve_key"]


def test_guarded_start_reports_and_records_the_guard(ctrl: FakeController, tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    started = _nerve_actionlog_start_sync(
        "S", str(path), target_package=TARGET, allow_packages="com.a"
    )

    assert started["guarded"] is True
    (header,) = _lines(path, "header")
    assert header["guard"]["target_package"] == TARGET
    assert "com.a" in header["guard"]["allowed_packages"]
    assert "buy" in header["guard"]["deny_words"]


# ---------------------------------------------------------------------------
# Pins — unguarded sessions and no session behave exactly as before
# ---------------------------------------------------------------------------


def test_unguarded_session_never_reads_foreground(ctrl: FakeController, tmp_path: Path) -> None:
    path = _start(tmp_path)
    ctrl.foreground = "com.android.vending"
    ctrl.label = {"text": "Buy"}

    assert input_tools._nerve_tap_sync("S", 150, 150)["success"] is True
    assert app_tools._nerve_launch_sync("S", "com.other.app")["success"] is True
    stopped = _nerve_actionlog_stop_sync("S")

    assert ctrl.foreground_calls == 0
    assert _tool_calls(ctrl, "tap") == [("S", 150, 150)]
    assert "guard_refusals" not in stopped
    assert _lines(path, "guard") == []
    (header,) = _lines(path, "header")
    assert "guard" not in header


def test_unguarded_start_result_has_no_guard_keys(ctrl: FakeController, tmp_path: Path) -> None:
    started = _nerve_actionlog_start_sync("S", str(tmp_path / "run.jsonl"))
    assert "guarded" not in started


def test_no_session_never_reads_foreground(ctrl: FakeController) -> None:
    ctrl.foreground = "com.android.vending"
    ctrl.label = {"text": "Buy"}

    assert input_tools._nerve_tap_sync("S", 150, 150)["success"] is True
    assert ctrl.foreground_calls == 0


# ---------------------------------------------------------------------------
# Fail closed — unreadable targets and label hierarchy (desk bounce)
# ---------------------------------------------------------------------------

_PARENT_WITH_BUY_CHILD = [
    {"class": "android.widget.FrameLayout", "bounds": "[0,0][1080,2400]"},
    {"class": "android.widget.LinearLayout", "bounds": "[100,100][500,300]", "clickable": True},
    {"class": "android.widget.TextView", "bounds": "[200,150][300,250]", "text": "Buy"},
]


class TestElementsAt:
    def test_every_containing_element_smallest_first(self) -> None:
        hits = action_log.elements_at(_PARENT_WITH_BUY_CHILD, 250, 200)
        assert [h["class_name"] for h in hits] == [
            "android.widget.TextView",
            "android.widget.LinearLayout",
            "android.widget.FrameLayout",
        ]

    def test_nested_children_are_walked(self) -> None:
        tree = [{"bounds": "[0,0][10,10]", "children": [{"bounds": "[1,1][5,5]", "text": "x"}]}]
        assert [h["text"] for h in action_log.elements_at(tree, 2, 2)] == ["x", ""]

    def test_nothing_under_the_point(self) -> None:
        assert action_log.elements_at(_PARENT_WITH_BUY_CHILD, 5000, 5000) == []


class TestGuardLabels:
    def test_descendant_of_clickable_container_counts(self) -> None:
        labels = action_log.guard_labels(_PARENT_WITH_BUY_CHILD, 120, 120)
        assert "Buy" in [label["text"] for label in labels]

    def test_non_clickable_container_descendants_do_not_count(self) -> None:
        assert action_log.guard_labels(_PARENT_WITH_BUY_CHILD, 900, 900) == [
            action_log.elements_at(_PARENT_WITH_BUY_CHILD, 900, 900)[0]
        ]


def test_tap_refused_when_element_read_fails(ctrl: FakeController, tmp_path: Path) -> None:
    path = _start(tmp_path, target_package=TARGET)
    ctrl.elements_error = RuntimeError("uiautomator dump killed")

    result = input_tools._nerve_tap_sync("S", 150, 150)

    assert result["guard"] == "target_unreadable"
    assert result["validation_error"] is True
    assert _tool_calls(ctrl, "tap") == []
    (line,) = _lines(path, "guard")
    assert line["reason"] == "target_unreadable"


def test_unguarded_tap_still_runs_when_element_read_fails(
    ctrl: FakeController, tmp_path: Path
) -> None:
    path = _start(tmp_path)
    ctrl.elements_error = RuntimeError("uiautomator dump killed")

    assert input_tools._nerve_tap_sync("S", 150, 150)["success"] is True
    (action,) = _lines(path, "action")
    assert action["target"] is None


def test_tap_in_parent_padding_around_buy_child_refused(
    ctrl: FakeController, tmp_path: Path
) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = _PARENT_WITH_BUY_CHILD

    result = input_tools._nerve_tap_sync("S", 120, 120)  # in the parent, outside the child

    assert result["guard"] == "denied_label"
    assert result["detail"] == "buy"
    assert _tool_calls(ctrl, "tap") == []


def test_unlabelled_icon_inside_subscribe_button_refused(
    ctrl: FakeController, tmp_path: Path
) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = [
        {"class": "android.widget.Button", "bounds": "[100,100][500,300]", "text": "Subscribe"},
        {"class": "android.widget.ImageView", "bounds": "[110,110][190,190]"},
    ]

    result = input_tools._nerve_tap_sync("S", 150, 150)

    assert result["guard"] == "denied_label"
    assert result["detail"] == "subscribe"
    assert _tool_calls(ctrl, "tap") == []


def test_unresolvable_serial_is_refused(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    with patch.object(_core, "_resolve_device", return_value=(None, "not found")):
        swipe = input_tools._nerve_swipe_sync("S", 1, 2, 3, 4)
        tap = input_tools._nerve_tap_sync("S", 150, 150)

    assert swipe["guard"] == "foreground_not_allowed"
    assert tap["guard"] == "foreground_not_allowed"
    assert _tool_calls(ctrl, "swipe") == [] and _tool_calls(ctrl, "tap") == []


# ---------------------------------------------------------------------------
# Round 3 (desk re-review): numeric key spellings, keys typed by nerve_type,
# in-button swipes, activation keys with nothing focused, gamepad fallbacks
# ---------------------------------------------------------------------------

_FRAME = {"class": "android.widget.FrameLayout", "bounds": "[0,0][1080,2400]"}
_FOCUSED_BUY = [
    _FRAME,
    {
        "class": "android.widget.Button",
        "bounds": "[100,100][500,300]",
        "text": "Buy",
        "clickable": True,
        "focused": True,
    },
]
_FOCUSED_EDIT_TEXT = [
    _FRAME,
    {"class": "android.widget.EditText", "bounds": "[0,500][1080,640]", "focused": True},
    {"class": "android.widget.Button", "bounds": "[100,100][500,300]", "text": "Buy"},
]
_NOTHING_FOCUSED = [
    _FRAME,
    {"class": "android.widget.Button", "bounds": "[100,100][500,300]", "text": "Buy"},
]
_FULL_WIDTH_BUY_NOW = [
    _FRAME,
    {
        "class": "android.widget.Button",
        "bounds": "[0,1000][1080,1200]",
        "text": "Buy now",
        "clickable": True,
    },
]


# 1. Numeric spellings — ``input keyevent`` parses "066" as 66 (ENTER), and
# KEYCODE_66 as 66 on API <= 28.


@pytest.mark.parametrize(
    "key",
    ["066", "KEYCODE_66", "0023", "023", "0160", "062", "keycode_23", "KEYCODE_0062"],
)
def test_numeric_activation_key_spellings_refused(
    ctrl: FakeController, tmp_path: Path, key: str
) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = _FOCUSED_BUY

    result = input_tools._nerve_key_sync("S", key)

    assert result["guard"] == "denied_label"
    assert result["detail"] == "buy"
    assert _tool_calls(ctrl, "press_key") == []


class TestNumericKeyNormalisation:
    @pytest.mark.parametrize("key", ["066", "KEYCODE_66", "0023", "0160", "062", "KEYCODE_23"])
    def test_numeric_activation(self, key: str) -> None:
        assert action_log.is_activation_key(key) is True

    @pytest.mark.parametrize("key", ["03", "004", "0003"])
    def test_zero_padded_bare_home_back_codes_are_escapes(self, key: str) -> None:
        """Bare numeric codes compare as integers: ``input keyevent 03`` is HOME."""
        assert action_log.is_escape_key(key) is True

    @pytest.mark.parametrize("key", ["KEYCODE_03", "KEYCODE_004", "0_3", "13", "30", "KEYCODE_3"])
    def test_prefixed_or_other_numbers_are_not_escapes(self, key: str) -> None:
        """Only bare numeric 3 / 4 escape; KEYCODE_<n> stays a digit (fail closed)."""
        assert action_log.is_escape_key(key) is False

    @pytest.mark.parametrize("key", ["0", "1", "KEYCODE_1", "660", "6_6", "KEYCODE_VOLUME_UP"])
    def test_other_numbers_are_not_activation(self, key: str) -> None:
        assert action_log.is_activation_key(key) is False


def test_zero_padded_home_runs_outside_the_allowed_apps(
    ctrl: FakeController, tmp_path: Path
) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.foreground = "com.android.vending"

    assert input_tools._nerve_key_sync("S", "03")["success"] is True


# Gamepad buttons fall back to DPAD_CENTER (Virtual.kcm / Generic.kcm, verified
# on an API 35 device); SELECT / MODE are classified fail closed.


@pytest.mark.parametrize(
    "key",
    [
        *("BUTTON_A", "KEYCODE_BUTTON_A", "BUTTON_C", "BUTTON_X", "BUTTON_Y", "BUTTON_Z"),
        *("BUTTON_START", "KEYCODE_BUTTON_START", "BUTTON_SELECT", "BUTTON_MODE"),
        *("BUTTON_THUMBL", "BUTTON_THUMBR", "BUTTON_1", "BUTTON_16", "NUMPAD_5"),
        *("96", "098", "108", "109", "110", "149", "188", "203"),
    ],
)
def test_gamepad_activation_keys_refused(ctrl: FakeController, tmp_path: Path, key: str) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = _FOCUSED_BUY

    result = input_tools._nerve_key_sync("S", key)

    assert result["guard"] == "denied_label"
    assert _tool_calls(ctrl, "press_key") == []


@pytest.mark.parametrize("key", ["BUTTON_B", "BUTTON_L1", "BUTTON_R2", "97", "102"])
def test_gamepad_non_activation_buttons(key: str) -> None:
    assert action_log.is_activation_key(key) is False


# 2. nerve_type presses keys — "\n" is ENTER, " " (sent as %s) is SPACE.


@pytest.mark.parametrize("text", ["\n", " ", "%s", "ok\n", "\r"])
def test_type_activation_chars_on_focused_buy_refused(
    ctrl: FakeController, tmp_path: Path, text: str
) -> None:
    path = _start(tmp_path, target_package=TARGET)
    ctrl.tree = _FOCUSED_BUY

    result = input_tools._nerve_type_sync("S", text)

    assert result["guard"] == "denied_label"
    assert result["detail"] == "buy"
    assert _tool_calls(ctrl, "type_text") == []
    (line,) = _lines(path, "guard")
    assert line["tool"] == "nerve_type"


def test_type_with_space_into_focused_edit_text_runs(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = _FOCUSED_EDIT_TEXT

    assert input_tools._nerve_type_sync("S", "a b")["success"] is True
    assert _tool_calls(ctrl, "type_text") == [("S", "a b")]


@pytest.mark.parametrize("text", ["a b", "\n"])
def test_type_activation_chars_with_nothing_focused_refused(
    ctrl: FakeController, tmp_path: Path, text: str
) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = _NOTHING_FOCUSED

    result = input_tools._nerve_type_sync("S", text)

    assert result["guard"] == "target_unreadable"
    assert result["detail"] == "no focused element"
    assert _tool_calls(ctrl, "type_text") == []


@pytest.mark.parametrize("text", ["a\t b", "x\n y", "\n\n"])
def test_type_moving_focus_before_an_activation_char_refused(
    ctrl: FakeController, tmp_path: Path, text: str
) -> None:
    """TAB / ENTER move focus off the field; a later SPACE / ENTER hits the next view."""
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = _FOCUSED_EDIT_TEXT

    result = input_tools._nerve_type_sync("S", text)

    assert result["guard"] == "target_unreadable"
    assert result["detail"] == "focus moves before an activation key"
    assert _tool_calls(ctrl, "type_text") == []


def test_type_plain_word_reads_no_elements(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.elements_error = AssertionError("plain text must not read the tree")

    assert input_tools._nerve_type_sync("S", "hello")["success"] is True


class TestTypedActivation:
    @pytest.mark.parametrize("text", ["\n", " ", "%s", "a b", "\r"])
    def test_needs_label_check(self, text: str) -> None:
        assert action_log.needs_label_check("nerve_type", {"text": text}) is True

    @pytest.mark.parametrize("text", ["hello", "", "a\tb", None])
    def test_no_label_check(self, text: Any) -> None:
        assert action_log.needs_label_check("nerve_type", {"text": text}) is False


# 3. A swipe that stays inside one clickable element is a click.


def test_in_button_swipe_on_buy_refused(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = _FULL_WIDTH_BUY_NOW

    result = input_tools._nerve_swipe_sync("S", 40, 1100, 540, 1100)  # 500 px, inside

    assert result["guard"] == "denied_label"
    assert result["detail"] == "buy"
    assert _tool_calls(ctrl, "swipe") == []


def test_swipe_leaving_the_buy_button_executes(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = _FULL_WIDTH_BUY_NOW

    assert input_tools._nerve_swipe_sync("S", 540, 1100, 540, 300)["success"] is True
    assert len(_tool_calls(ctrl, "swipe")) == 1


def test_guarded_long_swipe_with_unreadable_tree_refused(
    ctrl: FakeController, tmp_path: Path
) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.elements_error = RuntimeError("uiautomator dump killed")

    result = input_tools._nerve_swipe_sync("S", 540, 1100, 540, 300)

    assert result["guard"] == "target_unreadable"
    assert _tool_calls(ctrl, "swipe") == []


class TestInButtonSwipe:
    def test_needs_label_check_for_any_swipe(self) -> None:
        args = {"start_x": 0, "start_y": 0, "end_x": 900, "end_y": 900}
        assert action_log.needs_label_check("nerve_swipe", args) is True

    def test_malformed_swipe_needs_no_check(self) -> None:
        assert action_log.needs_label_check("nerve_swipe", {"start_x": "x"}) is False

    def test_click_point_is_the_start_inside_one_clickable(self) -> None:
        args = {"start_x": 40, "start_y": 1100, "end_x": 540, "end_y": 1100}
        assert action_log.click_point("nerve_swipe", args, _FULL_WIDTH_BUY_NOW) == (40, 1100)

    def test_no_click_point_without_a_clickable_container(self) -> None:
        args = {"start_x": 40, "start_y": 300, "end_x": 540, "end_y": 300}
        assert action_log.click_point("nerve_swipe", args, _FULL_WIDTH_BUY_NOW) is None


# 4. An activation key with nothing focused leaves touch mode and may focus
# (and activate) the first focusable view.


def test_activation_key_with_nothing_focused_refused(ctrl: FakeController, tmp_path: Path) -> None:
    path = _start(tmp_path, target_package=TARGET)
    ctrl.tree = _NOTHING_FOCUSED

    result = input_tools._nerve_key_sync("S", "ENTER")

    assert result["guard"] == "target_unreadable"
    assert result["detail"] == "no focused element"
    assert result["validation_error"] is True
    assert _tool_calls(ctrl, "press_key") == []
    (line,) = _lines(path, "guard")
    assert line["detail"] == "no focused element"
