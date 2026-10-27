"""Non-integer coordinates never slip past the guard (linc-nerve-vwkl review fix).

Swipe coordinates became ``int | None`` when the x1..y2 aliases landed. The
daemon's coercer only knew plain ``int``, and the guard's label check skipped a
swipe whose coordinates did not parse — so ``start_x="150.5"`` swiped a "Buy"
button in a guarded session. These tests pin every layer closed:

1. the pure guard helper refuses non-integer coordinates (fail closed);
2. a guarded session refuses them with a guard line, even past the daemon;
3. the daemon coerces / rejects ``X | None`` params like plain ``X``;
4. the tool body refuses them with no session at all;
5. the generated CLI types --start-x/--x1.. as INT and rejects 150.5.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from linc_nerve.core import action_log
from linc_nerve.daemon import http_router
from linc_nerve.mcp import _core, input_tools
from tests.test_guarded_session import TARGET, FakeController, _lines, _start, _tool_calls

_BAD = ("150.5", "1.5e2", 150.5, "abc", True)
_CANON = ("start_x", "start_y", "end_x", "end_y")
_ALIAS = ("x1", "y1", "x2", "y2")


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


def _swipe_body(names: tuple[str, ...], first: Any) -> dict[str, Any]:
    return {"device": "S", **dict(zip(names, (first, 150, 150, 150), strict=True))}


# ---------------------------------------------------------------------------
# 1. Pure helper — fail closed
# ---------------------------------------------------------------------------


class TestCoordinateRefusal:
    @pytest.mark.parametrize("bad", _BAD)
    def test_swipe_non_integer_coordinate_refused(self, bad: Any) -> None:
        args = {"start_x": bad, "start_y": 150, "end_x": 150, "end_y": 150}
        refusal = action_log.coordinate_refusal("nerve_swipe", args)
        assert refusal is not None
        assert refusal["validation_error"] is True
        assert refusal["guard"] == "target_unreadable"
        assert "start_x" in refusal["detail"]

    @pytest.mark.parametrize("tool", ["nerve_tap", "nerve_long_press"])
    def test_point_tools_non_integer_refused(self, tool: str) -> None:
        refusal = action_log.coordinate_refusal(tool, {"x": "1.5e2", "y": 10})
        assert refusal is not None and "x" in refusal["detail"]

    def test_missing_coordinate_refused(self) -> None:
        refusal = action_log.coordinate_refusal("nerve_swipe", {"start_x": 1, "start_y": 2})
        assert refusal is not None
        assert "end_x" in refusal["detail"] and "end_y" in refusal["detail"]

    @pytest.mark.parametrize("good", [0, "0", 150, "150", 150.0, 32767, "32767"])
    def test_integer_coordinates_pass(self, good: Any) -> None:
        args = {"start_x": good, "start_y": 1, "end_x": 2, "end_y": 3}
        assert action_log.coordinate_refusal("nerve_swipe", args) is None
        assert action_log.coordinate_refusal("nerve_tap", {"x": good, "y": 1}) is None

    def test_is_integer_coordinate_still_parses_signed_integers(self) -> None:
        assert action_log.is_integer_coordinate("-7") is True
        assert action_log.is_integer_coordinate(-1) is True
        assert action_log.is_integer_coordinate(10**30) is True
        assert action_log.is_integer_coordinate(1e30) is True

    @pytest.mark.parametrize(
        "out_of_range",
        [-1, "-1", "-7", -1.0, 32768, "32768", 32768.0, 40000, 10**30, "1" + "0" * 30, 1e30],
    )
    def test_coordinate_in_range_and_refusal_reject_out_of_range(self, out_of_range: Any) -> None:
        assert action_log.COORDINATE_MAX == 32767
        assert action_log.coordinate_in_range(out_of_range) is False
        refusal = action_log.coordinate_refusal("nerve_tap", {"x": out_of_range, "y": 150})
        assert refusal == {
            "error": (
                "guarded session: input refused, "
                "invalid coordinates (integers in 0..32767 required): x"
            ),
            "guard": "target_unreadable",
            "detail": "invalid coordinates (integers in 0..32767 required): x",
            "validation_error": True,
        }

    @pytest.mark.parametrize("bad", _BAD)
    def test_coordinate_in_range_rejects_non_integers(self, bad: Any) -> None:
        assert action_log.coordinate_in_range(bad) is False

    @pytest.mark.parametrize("good", [0, "0", 0.0, 150, 32767, "32767", 32767.0])
    def test_coordinate_in_range_accepts_bounds(self, good: Any) -> None:
        assert action_log.coordinate_in_range(good) is True

    def test_other_tools_never_refused(self) -> None:
        assert action_log.coordinate_refusal("nerve_type", {"text": "x"}) is None
        assert action_log.coordinate_refusal("nerve_key", {"key": "ENTER"}) is None

    def test_non_integer_names_lists_every_bad_key(self) -> None:
        args = {"start_x": "1.5", "start_y": 1, "end_x": None, "end_y": "x"}
        assert action_log.non_integer_coordinates("nerve_swipe", args) == [
            "start_x",
            "end_x",
            "end_y",
        ]
        assert action_log.non_integer_coordinates(
            "nerve_swipe", {"start_x": -1, "start_y": 0, "end_x": 32768, "end_y": 32767}
        ) == ["start_x", "end_x"]


# ---------------------------------------------------------------------------
# 2. Guarded session — refused with a guard line, never swiped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("names", [_CANON, _ALIAS], ids=["canonical", "alias"])
@pytest.mark.parametrize("bad", ["150.5", "1.5e2"])
def test_daemon_dispatch_guarded_buy_swipe_refused(
    ctrl: FakeController, tmp_path: Path, names: tuple[str, ...], bad: str
) -> None:
    """The reviewer's probe: daemon _dispatch_tool + guarded session + a Buy button."""
    _start(tmp_path, target_package=TARGET)
    ctrl.label = {"text": "Buy"}
    registry = {"nerve_swipe": input_tools._nerve_swipe_sync}

    result = http_router._dispatch_tool("nerve_swipe", _swipe_body(names, bad), registry)

    assert result["status"] == 400
    assert "Invalid parameters" in result["body"]["error"]
    assert names[0] in result["body"]["error"]
    assert _tool_calls(ctrl, "swipe") == []


@pytest.mark.parametrize("bad", ["150.5", "1.5e2", 150.5])
def test_guarded_session_refuses_non_integer_swipe_past_the_coercer(
    ctrl: FakeController, tmp_path: Path, bad: Any
) -> None:
    """Even if a caller skips the daemon coercer, the guard fails closed (guard line)."""
    path = _start(tmp_path, target_package=TARGET)
    ctrl.label = {"text": "Buy"}
    session = action_log.REGISTRY.active("S")
    assert session is not None
    raw = inspect.unwrap(input_tools._nerve_swipe_sync)
    kwargs = dict(zip(_CANON, (bad, 150, 150, 150), strict=True))

    result = _core._run_in_session(raw, "nerve_swipe", session, ("S",), kwargs)

    assert result["validation_error"] is True
    assert result["guard"] == "target_unreadable"
    assert _tool_calls(ctrl, "swipe") == []
    (guard,) = _lines(path, "guard")
    assert guard["tool"] == "nerve_swipe"
    assert "start_x" in guard["detail"]


@pytest.mark.parametrize("names", [_CANON, _ALIAS], ids=["canonical", "alias"])
def test_guarded_direct_call_non_integer_swipe_refused(
    ctrl: FakeController, tmp_path: Path, names: tuple[str, ...]
) -> None:
    path = _start(tmp_path, target_package=TARGET)
    ctrl.label = {"text": "Buy"}

    result = input_tools._nerve_swipe_sync(**_swipe_body(names, "150.5"))

    assert result["validation_error"] is True
    assert _tool_calls(ctrl, "swipe") == []
    assert len(_lines(path, "guard")) == 1


# ---------------------------------------------------------------------------
# 3. Daemon coercer — X | None and Optional[X] behave like X
# ---------------------------------------------------------------------------


def _optional_tool(
    device: str,
    a: int | None = None,
    b: Optional[int] = None,  # noqa: UP045
    c: float | None = None,
) -> dict[str, Any]:
    return {"a": a, "b": b, "c": c}


class TestOptionalCoercion:
    @pytest.mark.parametrize("field", ["a", "b"])
    @pytest.mark.parametrize("bad", ["150.5", "1.5e2", 1.5, "x", True, [1]])
    def test_optional_int_rejects_bad_value_with_400(self, field: str, bad: Any) -> None:
        result = http_router._coerce_typed_params(_optional_tool, {"device": "S", field: bad})
        assert result is not None
        assert result["status"] == 400
        assert result["body"]["error"].startswith(f"Invalid parameters: {field} must be int")

    @pytest.mark.parametrize("field", ["a", "b"])
    def test_optional_int_coerces_integer_string(self, field: str) -> None:
        body: dict[str, Any] = {"device": "S", field: "150"}
        assert http_router._coerce_typed_params(_optional_tool, body) is None
        assert body[field] == 150 and type(body[field]) is int

    def test_optional_accepts_none(self) -> None:
        body: dict[str, Any] = {"device": "S", "a": None, "b": None, "c": None}
        assert http_router._coerce_typed_params(_optional_tool, body) is None
        assert body == {"device": "S", "a": None, "b": None, "c": None}

    def test_optional_float_coerces_and_rejects(self) -> None:
        body: dict[str, Any] = {"device": "S", "c": "1.5"}
        assert http_router._coerce_typed_params(_optional_tool, body) is None
        assert body["c"] == 1.5
        bad = http_router._coerce_typed_params(_optional_tool, {"device": "S", "c": "1.5x"})
        assert bad is not None and "c must be float" in bad["body"]["error"]

    def test_wider_union_is_left_alone(self) -> None:
        def tool(device: str, v: int | str | None = None) -> dict[str, Any]:
            return {}

        body: dict[str, Any] = {"device": "S", "v": "1.5"}
        assert http_router._coerce_typed_params(tool, body) is None
        assert body["v"] == "1.5"


# ---------------------------------------------------------------------------
# 4. Tool body — refused with no session at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("names", [_CANON, _ALIAS], ids=["canonical", "alias"])
@pytest.mark.parametrize("bad", ["150.5", "1.5e2", 150.5])
def test_unsessioned_swipe_non_integer_is_validation_error(
    ctrl: FakeController, names: tuple[str, ...], bad: Any
) -> None:
    result = input_tools._nerve_swipe_sync(**_swipe_body(names, bad))
    assert result["validation_error"] is True
    assert "start_x" in result["error"]
    assert _tool_calls(ctrl, "swipe") == []


@pytest.mark.parametrize(
    ("call", "name"),
    [
        (lambda: input_tools._nerve_tap_sync("S", "1.5e2", 10), "tap"),
        (lambda: input_tools._nerve_long_press_sync("S", 10, "150.5"), "long_press"),
    ],
)
def test_unsessioned_point_tools_non_integer_is_validation_error(
    ctrl: FakeController, call: Any, name: str
) -> None:
    result = call()
    assert result["validation_error"] is True
    assert _tool_calls(ctrl, name) == []


def test_unsessioned_integer_string_swipe_still_runs(ctrl: FakeController) -> None:
    result = input_tools._nerve_swipe_sync("S", "150", 150, 150, 900)
    assert result["success"] is True


# ---------------------------------------------------------------------------
# 5. CLI — swipe coordinates are INT again
# ---------------------------------------------------------------------------


def test_cli_schema_types_swipe_coordinates_as_int() -> None:
    import json

    from linc_nerve.cli.main import main

    result = CliRunner().invoke(main, ["schema", "--json"])
    assert result.exit_code == 0, result.output
    options = json.loads(result.output)["commands"]["swipe"]["options"]
    for name in (*_CANON, *_ALIAS):
        assert options[name]["type"] == "INT", name


@pytest.mark.parametrize("flag", ["--start-x", "--x1"])
def test_cli_rejects_non_integer_swipe_coordinate(flag: str) -> None:
    from linc_nerve.cli.main import main

    rest = (
        ["--start-y", "1", "--end-x", "1", "--end-y", "1"]
        if flag == "--start-x"
        else ["--y1", "1", "--x2", "1", "--y2", "1"]
    )
    result = CliRunner().invoke(main, ["swipe", "--device", "S", flag, "150.5", *rest])
    assert result.exit_code == 2
    assert flag in result.output


# ---------------------------------------------------------------------------
# 6. Coordinate bounds 0..32767 — tool body, guarded session, daemon
# ---------------------------------------------------------------------------

_OUT_OF_RANGE_TAP_X: tuple[Any, ...] = (
    10**30,
    "1000000000000000000000000000000",
    1e30,
    -1,
    32768,
)


@pytest.mark.parametrize("bad_x", _OUT_OF_RANGE_TAP_X)
def test_unsessioned_tap_out_of_range_returns_validation_error(
    ctrl: FakeController, bad_x: Any
) -> None:
    result = input_tools._nerve_tap_sync("S", bad_x, 150)
    assert result == {
        "error": f"Invalid coordinates (integers in 0..32767 required): x={bad_x!r}",
        "validation_error": True,
    }
    assert _tool_calls(ctrl, "tap") == []


@pytest.mark.parametrize("bad_y", [-1, 32768, 10**30])
def test_unsessioned_long_press_out_of_range_returns_validation_error(
    ctrl: FakeController, bad_y: Any
) -> None:
    result = input_tools._nerve_long_press_sync("S", 150, bad_y)
    assert result == {
        "error": f"Invalid coordinates (integers in 0..32767 required): y={bad_y!r}",
        "validation_error": True,
    }
    assert _tool_calls(ctrl, "long_press") == []


@pytest.mark.parametrize(
    ("kwargs", "expected_shown"),
    [
        (
            {"start_x": 100, "start_y": 150, "end_x": 32768, "end_y": 150},
            "end_x=32768",
        ),
        (
            {"x1": 100, "y1": 150, "x2": 40000, "y2": 150},
            "end_x=40000",
        ),
    ],
)
def test_unsessioned_swipe_out_of_range_returns_validation_error(
    ctrl: FakeController, kwargs: dict[str, Any], expected_shown: str
) -> None:
    result = input_tools._nerve_swipe_sync("S", **kwargs)
    assert result == {
        "error": f"Invalid coordinates (integers in 0..32767 required): {expected_shown}",
        "validation_error": True,
    }
    assert _tool_calls(ctrl, "swipe") == []


@pytest.mark.parametrize("bound", [0, 32767])
def test_boundary_coordinates_0_and_32767_accepted(ctrl: FakeController, bound: int) -> None:
    tap_res = input_tools._nerve_tap_sync("S", bound, bound)
    assert tap_res["success"] is True
    lp_res = input_tools._nerve_long_press_sync("S", bound, bound)
    assert lp_res["success"] is True
    swipe_res = input_tools._nerve_swipe_sync("S", bound, 0, 32767, bound)
    assert swipe_res["success"] is True
    assert len(_tool_calls(ctrl, "tap")) == 1
    assert len(_tool_calls(ctrl, "long_press")) == 1
    assert len(_tool_calls(ctrl, "swipe")) == 1


@pytest.mark.parametrize("bad_x", _OUT_OF_RANGE_TAP_X)
def test_guarded_session_tap_out_of_range_refused_and_logged_once(
    ctrl: FakeController, tmp_path: Path, bad_x: Any
) -> None:
    path = _start(tmp_path, target_package=TARGET)
    ctrl.label = {"text": "OK"}

    result = input_tools._nerve_tap_sync("S", bad_x, 150)

    assert result == {
        "error": (
            "guarded session: input refused, "
            "invalid coordinates (integers in 0..32767 required): x"
        ),
        "guard": "target_unreadable",
        "detail": "invalid coordinates (integers in 0..32767 required): x",
        "validation_error": True,
    }
    assert _tool_calls(ctrl, "tap") == []
    guards = _lines(path, "guard")
    assert len(guards) == 1
    assert guards[0]["tool"] == "nerve_tap"
    assert guards[0]["detail"] == "invalid coordinates (integers in 0..32767 required): x"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_x": 100, "start_y": 150, "end_x": 32768, "end_y": 150},
        {"x1": 100, "y1": 150, "x2": 40000, "y2": 150},
    ],
    ids=["end_x_32768", "alias_x2_40000"],
)
def test_guarded_session_swipe_out_of_range_refused_and_logged_once(
    ctrl: FakeController, tmp_path: Path, kwargs: dict[str, Any]
) -> None:
    path = _start(tmp_path, target_package=TARGET)
    ctrl.label = {"text": "OK"}

    result = input_tools._nerve_swipe_sync("S", **kwargs)

    assert result == {
        "error": (
            "guarded session: input refused, "
            "invalid coordinates (integers in 0..32767 required): end_x"
        ),
        "guard": "target_unreadable",
        "detail": "invalid coordinates (integers in 0..32767 required): end_x",
        "validation_error": True,
    }
    assert _tool_calls(ctrl, "swipe") == []
    guards = _lines(path, "guard")
    assert len(guards) == 1
    assert guards[0]["tool"] == "nerve_swipe"
    assert guards[0]["detail"] == "invalid coordinates (integers in 0..32767 required): end_x"


@pytest.mark.parametrize(
    "bad_x",
    [10**30, "1000000000000000000000000000000", 1e30, -1, 32768],
)
def test_daemon_dispatch_tap_out_of_range_returns_validation_error(
    ctrl: FakeController, bad_x: Any
) -> None:
    registry = {"nerve_tap": input_tools._nerve_tap_sync}
    result = http_router._dispatch_tool(
        "nerve_tap", {"device": "S", "x": bad_x, "y": 150}, registry
    )
    assert result["status"] == 200
    inner = result["body"]["result"]
    assert inner["validation_error"] is True
    assert inner["error"].startswith("Invalid coordinates (integers in 0..32767 required): x=")
    assert _tool_calls(ctrl, "tap") == []


def test_daemon_dispatch_tap_scientific_string_rejected_400(ctrl: FakeController) -> None:
    registry = {"nerve_tap": input_tools._nerve_tap_sync}
    result = http_router._dispatch_tool(
        "nerve_tap", {"device": "S", "x": "1e30", "y": 150}, registry
    )
    assert result["status"] == 400
    assert "Invalid parameters: x must be int" in result["body"]["error"]
    assert _tool_calls(ctrl, "tap") == []
