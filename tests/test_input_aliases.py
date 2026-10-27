"""Tests for nerve_key/nerve_swipe aliases and nerve_navigate hint (linc-nerve-vwkl)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio
import pytest

from linc_nerve.cli.main import _registry_server
from linc_nerve.core import action_log
from linc_nerve.mcp import _core, input_tools, navigation_tools
from tests.test_guarded_session import FakeController


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


def _mcp_call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    srv = _registry_server()
    blocks = anyio.run(srv.call_tool, tool_name, arguments)
    if isinstance(blocks, tuple):
        blocks = blocks[0]
    return json.loads(blocks[0].text)


# ---------------------------------------------------------------------------
# 1. MCP strict_tools accepts keycode= and x1..y2 (and stringified ints)
# ---------------------------------------------------------------------------


def test_mcp_nerve_key_accepts_keycode_alias(ctrl: FakeController) -> None:
    res = _mcp_call("nerve_key", {"device": "S", "keycode": "ENTER"})
    assert res == {"success": True, "action": "key", "key": "ENTER"}
    assert ctrl.calls == [("press_key", ("S", "ENTER"))]


def test_mcp_nerve_key_accepts_matching_key_and_keycode(ctrl: FakeController) -> None:
    res = _mcp_call("nerve_key", {"device": "S", "key": "BACK", "keycode": "BACK"})
    assert res == {"success": True, "action": "key", "key": "BACK"}


def test_mcp_nerve_swipe_accepts_x1_y1_x2_y2_aliases(ctrl: FakeController) -> None:
    res = _mcp_call("nerve_swipe", {"device": "S", "x1": 10, "y1": 20, "x2": 30, "y2": 400})
    assert res == {"success": True, "action": "swipe", "from": [10, 20], "to": [30, 400]}
    assert ctrl.calls == [("swipe", ("S", 10, 20, 30, 400, 300))]


def test_mcp_nerve_swipe_coerces_stringified_ints(ctrl: FakeController) -> None:
    res = _mcp_call(
        "nerve_swipe",
        {"device": "S", "x1": "540", "y1": "1600", "x2": "540", "y2": "400", "duration_ms": "450"},
    )
    assert res == {"success": True, "action": "swipe", "from": [540, 1600], "to": [540, 400]}
    assert ctrl.calls == [("swipe", ("S", 540, 1600, 540, 400, 450))]


# ---------------------------------------------------------------------------
# 2. Validation errors for missing, conflicting, or mixed arguments
# ---------------------------------------------------------------------------


def test_nerve_key_missing_both_is_validation_error(ctrl: FakeController) -> None:
    res = input_tools._nerve_key_sync("S")
    assert res.get("validation_error") is True
    assert "key" in res["error"] and "keycode" in res["error"]
    assert ctrl.calls == []


def test_nerve_key_conflicting_key_and_keycode_is_validation_error(ctrl: FakeController) -> None:
    res = input_tools._nerve_key_sync("S", key="HOME", keycode="BACK")
    assert res.get("validation_error") is True
    assert "key" in res["error"] and "keycode" in res["error"]
    assert ctrl.calls == []


def test_nerve_swipe_missing_all_coordinates_is_validation_error(ctrl: FakeController) -> None:
    res = input_tools._nerve_swipe_sync("S")
    assert res.get("validation_error") is True
    for name in ("start_x", "start_y", "end_x", "end_y", "x1", "y1", "x2", "y2"):
        assert name in res["error"]
    assert ctrl.calls == []


def test_nerve_swipe_partial_canonical_names_missing(ctrl: FakeController) -> None:
    res = input_tools._nerve_swipe_sync("S", start_x=10, start_y=20)
    assert res.get("validation_error") is True
    assert "end_x" in res["error"] and "end_y" in res["error"]
    assert ctrl.calls == []


def test_nerve_swipe_partial_alias_names_missing(ctrl: FakeController) -> None:
    res = input_tools._nerve_swipe_sync("S", x1=10, y2=40)
    assert res.get("validation_error") is True
    assert "y1" in res["error"] and "x2" in res["error"]
    assert ctrl.calls == []


def test_nerve_swipe_mixed_canonical_and_alias_is_validation_error(ctrl: FakeController) -> None:
    res = input_tools._nerve_swipe_sync("S", start_x=10, start_y=20, x2=30, y2=40)
    assert res.get("validation_error") is True
    assert "end_x" in res["error"] and "end_y" in res["error"]
    assert "x1" in res["error"] and "y1" in res["error"]
    assert ctrl.calls == []


def test_nerve_swipe_both_complete_sets_is_validation_error(ctrl: FakeController) -> None:
    res = input_tools._nerve_swipe_sync(
        "S", start_x=1, start_y=2, end_x=3, end_y=4, x1=1, y1=2, x2=3, y2=4
    )
    assert res.get("validation_error") is True
    assert ctrl.calls == []


# ---------------------------------------------------------------------------
# 3. Pure helper _canonical_input_args
# ---------------------------------------------------------------------------


def test_canonical_input_args_passes_other_tools_unchanged() -> None:
    raw = {"device": "S", "x": 1, "y": 2}
    assert _core._canonical_input_args("nerve_tap", raw) == raw


# ---------------------------------------------------------------------------
# 4. nerve_navigate unknown-app error includes hint pointing at nerve_launch
# ---------------------------------------------------------------------------


def test_nerve_navigate_unknown_app_includes_launch_uri_hint() -> None:
    res = navigation_tools._nerve_navigate_sync("S", "com.some.unregistered", "home")
    assert res["validation_error"] is True
    assert "Unknown app 'com.some.unregistered'" in res["error"]
    assert res["hint"] == (
        "nerve_navigate only knows apps listed in its route registry "
        "(routes.yaml / NERVE_ROUTES_FILE). For any other app use "
        "nerve_launch(device, package, uri=<deep link>)."
    )


# ---------------------------------------------------------------------------
# 5. Coordinate bounds 0..32767 via MCP call_tool (canonical and x1..y2 alias)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_x",
    [10**30, "1000000000000000000000000000000", -1, 32768],
)
def test_mcp_nerve_tap_out_of_range_is_validation_error(ctrl: FakeController, bad_x: Any) -> None:
    res = _mcp_call("nerve_tap", {"device": "S", "x": bad_x, "y": 150})
    assert res == {
        "error": f"Invalid coordinates (integers in 0..32767 required): x={int(bad_x)!r}",
        "validation_error": True,
    }
    assert ctrl.calls == []


@pytest.mark.parametrize(
    ("args", "expected_shown"),
    [
        (
            {"device": "S", "start_x": 10, "start_y": 20, "end_x": 32768, "end_y": 40},
            "end_x=32768",
        ),
        (
            {"device": "S", "x1": 10, "y1": 20, "x2": 40000, "y2": 40},
            "end_x=40000",
        ),
    ],
)
def test_mcp_nerve_swipe_out_of_range_canonical_and_alias_is_validation_error(
    ctrl: FakeController, args: dict[str, Any], expected_shown: str
) -> None:
    res = _mcp_call("nerve_swipe", args)
    assert res == {
        "error": f"Invalid coordinates (integers in 0..32767 required): {expected_shown}",
        "validation_error": True,
    }
    assert ctrl.calls == []


def test_mcp_nerve_tap_and_swipe_accept_0_and_32767_bounds(ctrl: FakeController) -> None:
    assert _mcp_call("nerve_tap", {"device": "S", "x": 0, "y": 32767})["success"] is True
    assert (
        _mcp_call("nerve_swipe", {"device": "S", "x1": 0, "y1": 0, "x2": 32767, "y2": 32767})[
            "success"
        ]
        is True
    )
    assert ctrl.calls == [
        ("tap", ("S", 0, 32767)),
        ("swipe", ("S", 0, 0, 32767, 32767, 300)),
    ]
