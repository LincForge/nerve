"""Guarded-session swipe bypasses found on desk round-4 review (linc-nerve-2wh).

1. Touch slop: Android keeps a press on a view and clicks it on release while
   the finger stays within the view's touch slop, so a swipe ending just
   outside a clickable "Buy now" button is still a click on it.
2. Slide-to-confirm: a non-clickable SeekBar labelled "Slide to buy" is
   activated by a long swipe across it; a scroll inside a scrollable list is
   not an activation, whatever the list's label.

The fake controller from ``test_guarded_session`` stands in for adb/WDA; no
test spawns adb or talks to :8111.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from linc_nerve.core import action_log
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


_FRAME = {"class": "android.widget.FrameLayout", "bounds": "[0,0][1080,2400]"}
_BUY_NOW = [
    _FRAME,
    {
        "class": "android.widget.Button",
        "bounds": "[0,1000][900,1200]",
        "text": "Buy now",
        "clickable": True,
    },
]
_SLIDE_TO_BUY = [
    _FRAME,
    {
        "class": "android.widget.SeekBar",
        "bounds": "[20,1000][1060,1200]",
        "desc": "Slide to buy",
    },
]
_ORDER_HISTORY = [
    _FRAME,
    {
        "class": "androidx.recyclerview.widget.RecyclerView",
        "bounds": "[0,200][1080,2200]",
        "desc": "Order history",
        "scrollable": True,
    },
]


# ---------------------------------------------------------------------------
# 1. Touch slop — a swipe ending just outside the button still clicks it
# ---------------------------------------------------------------------------


def test_swipe_ending_within_touch_slop_of_buy_refused(
    ctrl: FakeController, tmp_path: Path
) -> None:
    path = _start(tmp_path, target_package=TARGET)
    ctrl.tree = _BUY_NOW

    result = input_tools._nerve_swipe_sync("S", 40, 1100, 915, 1100)  # 15 px past the edge

    assert result["guard"] == "denied_label"
    assert result["detail"] == "buy"
    assert _tool_calls(ctrl, "swipe") == []
    (line,) = _lines(path, "guard")
    assert line["reason"] == "denied_label"


def test_swipe_ending_beyond_touch_slop_executes(ctrl: FakeController, tmp_path: Path) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = _BUY_NOW

    result = input_tools._nerve_swipe_sync("S", 40, 1100, 960, 1100)  # 60 px past the edge

    assert result["success"] is True
    assert len(_tool_calls(ctrl, "swipe")) == 1


@pytest.mark.parametrize(
    ("end_x", "end_y"),
    [(948, 1100), (450, 952), (450, 1248), (948, 1248)],
)
def test_click_point_at_the_touch_slop_edge(end_x: int, end_y: int) -> None:
    args = {"start_x": 40, "start_y": 1100, "end_x": end_x, "end_y": end_y}
    assert action_log.click_point("nerve_swipe", args, _BUY_NOW) == (40, 1100)


@pytest.mark.parametrize(
    ("end_x", "end_y"),
    [(949, 1100), (450, 951), (450, 1249)],
)
def test_no_click_point_past_the_touch_slop_edge(end_x: int, end_y: int) -> None:
    args = {"start_x": 40, "start_y": 1100, "end_x": end_x, "end_y": end_y}
    assert action_log.click_point("nerve_swipe", args, _BUY_NOW) is None


# ---------------------------------------------------------------------------
# 2. Slide-to-confirm — a swipe across a deny-labelled non-scrollable control
# ---------------------------------------------------------------------------


def test_slide_to_buy_seekbar_swipe_refused(ctrl: FakeController, tmp_path: Path) -> None:
    path = _start(tmp_path, target_package=TARGET)
    ctrl.tree = _SLIDE_TO_BUY

    result = input_tools._nerve_swipe_sync("S", 40, 1100, 1040, 1100)  # 1000 px across

    assert result["guard"] == "denied_label"
    assert result["detail"] == "buy"
    assert _tool_calls(ctrl, "swipe") == []
    (line,) = _lines(path, "guard")
    assert line["reason"] == "denied_label"


def test_scroll_inside_scrollable_order_history_executes(
    ctrl: FakeController, tmp_path: Path
) -> None:
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = _ORDER_HISTORY

    result = input_tools._nerve_swipe_sync("S", 540, 1800, 540, 600)

    assert result["success"] is True
    assert len(_tool_calls(ctrl, "swipe")) == 1


def test_swipe_leaving_the_slide_control_executes(ctrl: FakeController, tmp_path: Path) -> None:
    """Known gap: only a swipe whose both ends lie on the control is checked."""
    _start(tmp_path, target_package=TARGET)
    ctrl.tree = _SLIDE_TO_BUY

    assert input_tools._nerve_swipe_sync("S", 540, 1100, 540, 300)["success"] is True


class TestSpannedLabels:
    def test_non_scrollable_elements_holding_both_ends(self) -> None:
        args = {"start_x": 40, "start_y": 1100, "end_x": 1040, "end_y": 1100}
        labels = action_log.swipe_span_labels(args, _SLIDE_TO_BUY)
        assert [label["content_desc"] for label in labels] == ["Slide to buy", ""]

    def test_scroll_skips_the_scrollable_and_its_ancestors(self) -> None:
        args = {"start_x": 540, "start_y": 1800, "end_x": 540, "end_y": 600}
        assert action_log.swipe_span_labels(args, _ORDER_HISTORY) == []

    def test_short_swipe_spans_nothing(self) -> None:
        args: dict[str, Any] = {"start_x": 40, "start_y": 1100, "end_x": 60, "end_y": 1100}
        assert action_log.swipe_span_labels(args, _SLIDE_TO_BUY) == []

    def test_malformed_swipe_spans_nothing(self) -> None:
        assert action_log.swipe_span_labels({"start_x": "x"}, _SLIDE_TO_BUY) == []


# ---------------------------------------------------------------------------
# 3. Scrolls — a swipe inside a scrollable container scrolls it; only a slider
#    child (SeekBar / Slider / RatingBar) spanning both ends is an activation
# ---------------------------------------------------------------------------

_SCROLL_BOUNDS = "[0,200][1080,2200]"


def _in_scrollable(container_class: str, child: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _FRAME,
        {
            "class": container_class,
            "bounds": _SCROLL_BOUNDS,
            "scrollable": True,
            "children": [child],
        },
    ]


_ARTICLE_SCROLL = _in_scrollable(
    "android.widget.ScrollView",
    {
        "class": "android.widget.TextView",
        "bounds": _SCROLL_BOUNDS,
        "text": "Read our latest post and share it with your friends",
    },
)
_FEED_CARD = _in_scrollable(
    "androidx.recyclerview.widget.RecyclerView",
    {
        "class": "android.view.ViewGroup",
        "bounds": "[0,200][1080,1900]",
        "desc": "Alice. Reply, Repost, Like, Share",
    },
)
_UPGRADE_PAGE = _in_scrollable(
    "androidx.viewpager.widget.ViewPager",
    {
        "class": "android.widget.FrameLayout",
        "bounds": _SCROLL_BOUNDS,
        "desc": "Upgrade to Pro",
    },
)
_SCROLLED_SLIDE_TO_BUY = _in_scrollable(
    "android.widget.ScrollView",
    {
        "class": "android.widget.SeekBar",
        "bounds": "[20,1000][1060,1200]",
        "desc": "Slide to buy",
    },
)


@pytest.mark.parametrize(
    ("tree", "swipe"),
    [
        pytest.param(_ARTICLE_SCROLL, (540, 1800, 540, 600), id="scrollview-post"),
        pytest.param(_FEED_CARD, (540, 1700, 540, 500), id="recyclerview-share"),
        pytest.param(_UPGRADE_PAGE, (1000, 1200, 80, 1200), id="viewpager-upgrade"),
    ],
)
def test_scroll_over_deny_labelled_child_executes(
    ctrl: FakeController,
    tmp_path: Path,
    tree: list[dict[str, Any]],
    swipe: tuple[int, int, int, int],
) -> None:
    path = _start(tmp_path, target_package=TARGET)
    ctrl.tree = tree

    result = input_tools._nerve_swipe_sync("S", *swipe)

    assert result.get("success") is True, result
    assert len(_tool_calls(ctrl, "swipe")) == 1
    assert _lines(path, "guard") == []


def test_slide_to_buy_seekbar_inside_scrollview_refused(
    ctrl: FakeController, tmp_path: Path
) -> None:
    path = _start(tmp_path, target_package=TARGET)
    ctrl.tree = _SCROLLED_SLIDE_TO_BUY

    result = input_tools._nerve_swipe_sync("S", 40, 1100, 1040, 1100)

    assert result["guard"] == "denied_label"
    assert result["detail"] == "buy"
    assert _tool_calls(ctrl, "swipe") == []
    (line,) = _lines(path, "guard")
    assert line["reason"] == "denied_label"


class TestScrollSpannedLabels:
    def test_scroll_spans_only_sliders(self) -> None:
        args = {"start_x": 40, "start_y": 1100, "end_x": 1040, "end_y": 1100}
        labels = action_log.swipe_span_labels(args, _SCROLLED_SLIDE_TO_BUY)
        assert [label["class_name"] for label in labels] == ["android.widget.SeekBar"]

    def test_scroll_over_plain_child_spans_nothing(self) -> None:
        args = {"start_x": 540, "start_y": 1800, "end_x": 540, "end_y": 600}
        assert action_log.swipe_span_labels(args, _ARTICLE_SCROLL) == []

    def test_scrollable_holding_only_the_start_is_not_a_scroll(self) -> None:
        """A scrollable that the swipe leaves does not demote the span check."""
        tree = [
            _FRAME,
            _SLIDE_TO_BUY[1],
            {
                "class": "android.widget.HorizontalScrollView",
                "bounds": "[0,1000][500,1200]",
                "scrollable": True,
            },
        ]
        args = {"start_x": 40, "start_y": 1100, "end_x": 1040, "end_y": 1100}
        labels = action_log.swipe_span_labels(args, tree)
        assert [label["content_desc"] for label in labels] == ["Slide to buy", ""]
