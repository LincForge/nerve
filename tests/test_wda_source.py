"""Contract and property tests for ``linc_nerve.core.wda_source`` (linc-nerve-9cvm)."""

from __future__ import annotations

import random

import pytest

import linc_nerve.core.ios  # noqa: F401 — break conftest ios_discovery circular import on standalone run
import linc_nerve.core.wda_source as wda_source
from linc_nerve.core.wda_source import (
    _BOUNDS_RE,
    _CONTAINER_TYPES,
    _FRAME_RE,
    _MAX_SCANNED_NODES,
    _TAPPABLE_TYPES,
    _extract_identifier,
    _extract_text,
    _extract_viewport,
    _is_clickable,
    _parse_frame,
    _parse_rect,
    parse_wda_source,
)


def _el(
    el_type: str,
    rid: str = "",
    label: str = "",
    frame: str = "",
    rect: dict | None = None,
    children: object = None,
    **extra: object,
) -> dict:
    node: dict = {"type": el_type, "rawIdentifier": rid, "label": label}
    if frame:
        node["frame"] = frame
    if rect is not None:
        node["rect"] = rect
    node["children"] = [] if children is None else children
    node.update(extra)
    return node


class TestSignedCoordinates:
    """Contract (1): negative coordinates in WDA frame strings and rect dicts."""

    def test_parse_frame_preserves_negative_coordinates(self) -> None:
        assert _parse_frame("{{60, -16475}, {277, 123}}") == "[60,-16475][337,-16352]"
        assert _parse_frame("{{-15.5, -20.0}, {100.0, 50.0}}") == "[-15,-20][85,30]"

    def test_frame_regex_matches_signed_numbers(self) -> None:
        assert _FRAME_RE.findall("{{60, -16475}, {277, 123}}") == ["60", "-16475", "277", "123"]

    def test_parse_rect_preserves_negative_float_coordinates(self) -> None:
        rect = {"x": -12.0, "y": -300.6, "width": 100.0, "height": 44.0}
        assert _parse_rect(rect) == "[-12,-300][88,-256]"

    def test_parse_wda_source_preserves_negative_rect_and_frame(self) -> None:
        neg_rect = {"x": -12.0, "y": -300.6, "width": 100.0, "height": 44.0}
        tree = {
            "type": "XCUIElementTypeApplication",
            "children": [
                _el(
                    "StaticText", "offscreen_frame", "History", frame="{{60, -16475}, {277, 123}}"
                ),
                _el("StaticText", "", "Earlier", rect=neg_rect, name="offscreen_rect"),
            ],
        }
        result = parse_wda_source(tree)
        assert len(result) == 2
        assert result[0]["bounds"] == "[60,-16475][337,-16352]"
        assert result[1]["bounds"] == "[-12,-300][88,-256]"
        assert [e["index"] for e in result] == [0, 1]


class TestViewportPrioritisation:
    """Contract (2): viewport-intersecting elements prioritised ahead of off-screen scrollback."""

    def test_onscreen_controls_prioritised_ahead_of_offscreen_scrollback(self) -> None:
        offscreen_nodes = [
            _el(
                "StaticText",
                f"msg_{i}",
                f"Message {i}",
                f"{{{{16, {-12000 + i * 99}}}, {{300, 40}}}}",
            )
            for i in range(120)
        ]
        assert -12000 + 119 * 99 < -100

        onscreen_controls = [
            _el(
                "Button" if i < 3 else "Tab",
                f"nav_{i}",
                f"Control {i}",
                f"{{{{{20 + i * 70}, 780}}, {{60, 49}}}}",
                isEnabled="1",
            )
            for i in range(5)
        ]

        tree = {
            "type": "XCUIElementTypeApplication",
            "frame": "{{0, 0}, {390, 844}}",
            "children": offscreen_nodes + onscreen_controls,
        }

        result = parse_wda_source(tree, max_elements=80)
        assert len(result) == 80
        assert [e["index"] for e in result] == list(range(80))
        assert [e["id"] for e in result[:5]] == [f"nav_{i}" for i in range(5)]
        for i in range(3):
            assert result[i]["class"] == "Button"
            assert result[i]["clickable"] is True
        for i in range(3, 5):
            assert result[i]["class"] == "Tab"
            assert result[i]["clickable"] is True
        assert [e["id"] for e in result[5:]] == [f"msg_{i}" for i in range(75)]

    def test_root_rect_viewport_prioritises_onscreen_elements(self) -> None:
        tree = {
            "type": "Application",
            "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
            "children": [
                _el(
                    "StaticText",
                    label="Above",
                    rect={"x": 10, "y": -50, "width": 100, "height": 40},
                ),
                _el(
                    "Button", label="Visible", rect={"x": 10, "y": 100, "width": 100, "height": 40}
                ),
            ],
        }
        result = parse_wda_source(tree, max_elements=80)
        assert [e["text"] for e in result] == ["Visible", "Above"]
        assert [e["index"] for e in result] == [0, 1]

    @pytest.mark.parametrize(
        ("frame", "expected_in_viewport"),
        [
            ("{{0, 0}, {10, 10}}", True),
            ("{{-5, 10}, {6, 10}}", True),
            ("{{389, 10}, {5, 10}}", True),
            ("{{10, -5}, {10, 6}}", True),
            ("{{10, 843}, {10, 5}}", True),
            ("{{-10, 10}, {10, 10}}", False),
            ("{{390, 10}, {10, 10}}", False),
            ("{{10, -10}, {10, 10}}", False),
            ("{{10, 844}, {10, 10}}", False),
            ("{{10, 10}, {0, 10}}", False),
            ("{{10, 10}, {10, 0}}", False),
            ("{{10, 10}, {-5, 10}}", False),
            ("{{10, 10}, {10, -5}}", False),
        ],
    )
    def test_viewport_intersection_boundaries(
        self, frame: str, expected_in_viewport: bool
    ) -> None:
        tree = {
            "type": "XCUIElementTypeApplication",
            "frame": "{{0, 0}, {390, 844}}",
            "children": [
                _el("StaticText", label="offscreen_anchor", frame="{{0, -500}, {50, 50}}"),
                _el("StaticText", label="candidate", frame=frame),
            ],
        }
        result = parse_wda_source(tree, max_elements=80)
        expected = (
            ["candidate", "offscreen_anchor"]
            if expected_in_viewport
            else ["offscreen_anchor", "candidate"]
        )
        assert [e["text"] for e in result] == expected


class TestScanCap:
    """Contract (3): synthetic tree with 5,000 child nodes stops scanning at _MAX_SCANNED_NODES."""

    def test_scan_cap_constant_is_2500(self) -> None:
        assert wda_source._MAX_SCANNED_NODES == 2500
        assert _MAX_SCANNED_NODES == 2500

    def test_synthetic_5000_node_tree_stops_at_max_scanned_nodes(self) -> None:
        cap = _MAX_SCANNED_NODES
        children = [
            _el(
                "StaticText",
                f"node_{i}",
                f"Node {i}",
                f"{{{{10, {-5000 + i}}}, {{100, 20}}}}" if i < cap else "{{10, 100}, {100, 20}}",
            )
            for i in range(5000)
        ]
        tree = {
            "type": "XCUIElementTypeApplication",
            "frame": "{{0, 0}, {390, 844}}",
            "children": children,
        }

        result_all = parse_wda_source(tree, max_elements=5000)
        assert len(result_all) == cap
        assert result_all[-1]["id"] == f"node_{cap - 1}"
        assert [e["index"] for e in result_all] == list(range(cap))

        result_default = parse_wda_source(tree, max_elements=80)
        assert len(result_default) == 80
        assert all(int(e["id"].split("_")[1]) < cap for e in result_default)


class TestHostileInputs:
    """Contract (4): hostile / non-finite / malformed inputs never crash."""

    def test_hostile_frames_and_rects_return_empty_bounds(self) -> None:
        assert _parse_frame("") == ""
        assert _parse_frame("{{nan, inf}, {10, 10}}") == ""
        assert _parse_frame(f"{{{{0, 0}}, {{10, {'9' * 400}.0}}}}") == ""
        assert _parse_frame("{{10, 20}}") == ""
        assert _parse_frame(None) == ""  # type: ignore[arg-type]
        assert _parse_rect({}) == ""
        assert _parse_rect(None) == ""  # type: ignore[arg-type]
        assert _parse_rect({"x": float("nan"), "y": 0, "width": 10, "height": 10}) == ""
        assert _parse_rect({"x": float("inf"), "y": 0, "width": 10, "height": 10}) == ""
        assert _parse_rect({"x": float("-inf"), "y": 0, "width": 10, "height": 10}) == ""
        assert _parse_rect({"x": "bad"}) == ""
        assert _parse_rect({"x": None, "y": 0, "width": 10, "height": 10}) == ""

    def test_unparseable_child_bounds_under_valid_viewport_placed_in_offscreen_partition(
        self,
    ) -> None:
        tree = {
            "type": "XCUIElementTypeApplication",
            "frame": "{{0, 0}, {390, 844}}",
            "children": [
                _el("StaticText", "bad_bounds", "NoBounds", frame="{{nan, inf}, {10, 10}}"),
                _el("Button", "good_bounds", "Visible", frame="{{10, 100}, {100, 44}}"),
            ],
        }
        result = parse_wda_source(tree, max_elements=80)
        assert [e["id"] for e in result] == ["good_bounds", "bad_bounds"]
        assert [e["index"] for e in result] == [0, 1]

    def test_hostile_tree_with_zero_root_viewport_preserves_document_order(self) -> None:
        nan_rect = {"x": float("nan"), "y": 0, "width": 10, "height": 10}
        tree = {
            "type": "XCUIElementTypeApplication",
            "frame": "{{0, 0}, {0, 0}}",
            "children": [
                None,
                "str",
                _el(
                    "StaticText",
                    "nan_frame",
                    "First",
                    frame="{{nan, inf}, {10, 10}}",
                    children=[None, "str"],
                ),
                _el("StaticText", "nan_rect", "Second", rect=nan_rect),
                {
                    "type": "Button",
                    "rawIdentifier": "bad_rect",
                    "label": "Third",
                    "rect": {"x": "bad"},
                    "children": None,
                },
                _el("Button", "onscreen_btn", "Fourth", frame="{{10, 100}, {100, 44}}"),
            ],
        }
        result = parse_wda_source(tree, max_elements=3)
        assert len(result) == 3
        assert [e["id"] for e in result] == ["nan_frame", "nan_rect", "bad_rect"]
        assert [e["index"] for e in result] == [0, 1, 2]
        assert all(isinstance(e, dict) for e in result)
        assert all("bounds" not in e for e in result)

    def test_non_dict_root_or_non_positive_max_elements(self) -> None:
        assert parse_wda_source(None) == []  # type: ignore[arg-type]
        assert (
            parse_wda_source(
                {"type": "Application", "children": [{"type": "Button", "label": "OK"}]},
                max_elements=0,
            )
            == []
        )


class TestWDASourceProperty:
    """Contract (5) [property]: signed bounds round-trip, contiguous indices, viewport priority."""

    @pytest.mark.parametrize("seed", range(40))
    def test_generated_wda_trees_preserve_sign_indices_and_viewport_priority(
        self, seed: int
    ) -> None:
        rng = random.Random(seed)
        win_w, win_h = 390, 844
        num_nodes = rng.randint(0, 160)
        max_elements = rng.choice([1, 10, 40, 80, 120])

        children: list[dict] = []
        expected_coords: dict[str, tuple[int, int, int, int]] = {}
        onscreen_ids: list[str] = []

        for i in range(num_nodes):
            if rng.random() < 0.35:
                x, y = rng.randint(-100, 500), rng.randint(-200, 1000)
            else:
                x, y = rng.randint(-20000, 20000), rng.randint(-20000, 20000)
            w, h = rng.randint(1, 400), rng.randint(1, 400)
            node_id = f"el_{i}"
            node: dict = {
                "type": rng.choice(["StaticText", "Button", "Tab", "Cell"]),
                "rawIdentifier": node_id,
                "label": f"Label {i}",
                "children": [],
            }
            if rng.random() < 0.3:
                node["rect"] = {"x": x, "y": y, "width": w, "height": h}
            else:
                node["frame"] = f"{{{{{x}, {y}}}, {{{w}, {h}}}}}"

            children.append(node)
            x2, y2 = x + w, y + h
            expected_coords[node_id] = (x, y, x2, y2)
            if x2 > 0 and x < win_w and y2 > 0 and y < win_h:
                onscreen_ids.append(node_id)

        tree = {
            "type": "XCUIElementTypeApplication",
            "frame": f"{{{{0, 0}}, {{{win_w}, {win_h}}}}}",
            "children": children,
        }

        result = parse_wda_source(tree, max_elements=max_elements)
        assert len(result) <= max_elements
        assert len(result) == min(num_nodes, max_elements)
        assert [e["index"] for e in result] == list(range(len(result)))

        returned_ids: list[str] = []
        seen_offscreen = False
        for elem in result:
            eid = elem["id"]
            returned_ids.append(eid)
            m = _BOUNDS_RE.fullmatch(elem["bounds"])
            assert m is not None
            bx1, by1, bx2, by2 = (int(m.group(i)) for i in range(1, 5))
            ex1, ey1, ex2, ey2 = expected_coords[eid]
            assert (bx1, by1, bx2, by2) == (ex1, ey1, ex2, ey2)
            assert (bx1 < 0) == (ex1 < 0)
            assert (by1 < 0) == (ey1 < 0)

            in_viewport = bx2 > 0 and bx1 < win_w and by2 > 0 and by1 < win_h
            if not in_viewport:
                seen_offscreen = True
            else:
                assert not seen_offscreen

        assert num_nodes <= _MAX_SCANNED_NODES
        if set(onscreen_ids) - set(returned_ids):
            assert not seen_offscreen


class TestWDASourceHelpers:
    """Direct unit tests for wda_source helpers and edge cases."""

    def test_extract_identifier_and_text_and_clickable(self) -> None:
        assert _extract_identifier({"rawIdentifier": "raw", "name": "nm"}) == "raw"
        assert _extract_identifier({"name": "nm"}) == "nm"
        assert _extract_identifier({}) == ""
        assert _extract_text({"label": "lbl", "value": "val"}) == "lbl"
        assert _extract_text({"label": "", "value": "val"}) == "val"
        assert _extract_text({}) == ""
        for t in _TAPPABLE_TYPES:
            assert _is_clickable(t, True) is True
            assert _is_clickable(t, False) is False
        for c in _CONTAINER_TYPES:
            assert _is_clickable(c, True) is False

    def test_content_desc_and_depth_and_disabled(self) -> None:
        deep_child = _el("Button", "deep", "Deep", frame="{{10, 10}, {50, 20}}")
        tree = {
            "type": "Application",
            "children": [
                _el("StaticText", "same", "same", frame="{{0, 0}, {50, 20}}"),
                _el(
                    "Button",
                    "diff_id",
                    "diff_text",
                    frame="{{0, 20}, {50, 20}}",
                    children=[deep_child],
                    isEnabled="0",
                ),
            ],
        }
        result = parse_wda_source(tree, max_depth=1)
        assert len(result) == 2
        assert "desc" not in result[0]
        assert result[1]["desc"] == "diff_id"
        assert result[1]["enabled"] is False
        assert "clickable" not in result[1]


class TestZeroBudgetShortCircuit:
    """[contract] ``max_elements <= 0`` returns ``[]`` without scanning the tree."""

    @pytest.mark.parametrize("max_elements", [0, -1])
    def test_non_positive_budget_never_walks_tree(
        self, monkeypatch: pytest.MonkeyPatch, max_elements: int
    ) -> None:
        calls: list[int] = []
        monkeypatch.setattr(wda_source, "_walk", lambda *a, **k: calls.append(1))
        tree = {"type": "Application", "children": [{"type": "Button", "label": "OK"}]}
        assert parse_wda_source(tree, max_elements=max_elements) == []
        assert calls == []

    def test_positive_budget_walks_tree(self) -> None:
        tree = {"type": "Application", "children": [{"type": "Button", "label": "OK"}]}
        assert [e["text"] for e in parse_wda_source(tree, max_elements=1)] == ["OK"]


class TestFrameTypeGuard:
    """[contract] a non-str frame (even a truthy one) yields ``""`` instead of raising."""

    @pytest.mark.parametrize("frame", [12345, b"{{1, 2}, {3, 4}}", ["{{1, 2}, {3, 4}}"], 1.5])
    def test_truthy_non_str_frame_returns_empty(self, frame: object) -> None:
        assert _parse_frame(frame) == ""  # type: ignore[arg-type]

    @pytest.mark.parametrize("frame", ["", None, 0, b""])
    def test_falsy_frame_returns_empty(self, frame: object) -> None:
        assert _parse_frame(frame) == ""  # type: ignore[arg-type]


class TestRectMissingKeysDefaultToZero:
    """[contract] each missing rect key defaults to 0 (never None, never 1)."""

    @pytest.mark.parametrize(
        ("missing", "expected"),
        [
            ("x", "[0,7][11,20]"),
            ("y", "[5,0][16,13]"),
            ("width", "[5,7][5,20]"),
            ("height", "[5,7][16,7]"),
        ],
    )
    def test_single_missing_key_defaults_to_zero(self, missing: str, expected: str) -> None:
        rect = {"x": 5, "y": 7, "width": 11, "height": 13}
        del rect[missing]
        assert _parse_rect(rect) == expected

    def test_only_unrelated_keys_default_every_field_to_zero(self) -> None:
        assert _parse_rect({"unrelated": 1}) == "[0,0][0,0]"


class TestExtractViewport:
    """[contract] viewport is (x2 - x1, y2 - y1) and requires both dimensions > 0."""

    def test_non_zero_origin_uses_width_and_height_not_far_edge(self) -> None:
        assert _extract_viewport({"frame": "{{100, 50}, {390, 844}}"}) == (390, 844)
        assert _extract_viewport({"rect": {"x": -30, "y": -40, "width": 390, "height": 844}}) == (
            390,
            844,
        )

    @pytest.mark.parametrize(
        ("frame", "expected"),
        [
            ("{{0, 0}, {0, 844}}", None),
            ("{{0, 0}, {390, 0}}", None),
            ("{{0, 0}, {0, 0}}", None),
            ("{{0, 0}, {-5, 844}}", None),
            ("{{0, 0}, {390, -5}}", None),
            ("{{0, 0}, {1, 844}}", (1, 844)),
            ("{{0, 0}, {390, 1}}", (390, 1)),
            ("{{0, 0}, {1, 1}}", (1, 1)),
        ],
    )
    def test_dimension_boundaries(self, frame: str, expected: tuple[int, int] | None) -> None:
        assert _extract_viewport({"frame": frame}) == expected

    def test_missing_bounds_yields_no_viewport(self) -> None:
        assert _extract_viewport({"type": "Application"}) is None


class TestEnabledPropagation:
    """[contract] the parsed isEnabled flag reaches the element dict in both directions."""

    @pytest.mark.parametrize(
        ("extra", "enabled"),
        [
            ({}, True),
            ({"isEnabled": True}, True),
            ({"isEnabled": "1"}, True),
            ({"isEnabled": False}, False),
            ({"isEnabled": "0"}, False),
            ({"isEnabled": 0}, False),
        ],
    )
    def test_enabled_flag_propagates(self, extra: dict, enabled: bool) -> None:
        tree = {"type": "Application", "children": [_el("StaticText", "lbl", "Text", **extra)]}
        (elem,) = parse_wda_source(tree)
        assert elem.get("enabled", True) is enabled
        assert ("enabled" in elem) is (not enabled)
