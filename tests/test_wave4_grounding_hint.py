"""[contract] Wave 4 visual_grounding_hint ratio and off-centre mask tests (linc-nerve-9s48)."""

from __future__ import annotations

import importlib.util
import io
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from linc_nerve.core import grounding
from linc_nerve.mcp import _core, input_tools, ui_tools

# The parser is the optional `[grounding]` extra (Meta SAM License); the public
# release self-check installs only `[dev]`, so the mask test skips there and runs
# in CI (`uv sync --all-extras`) -- same gate as tests/test_grounding.py.
needs_grounding_extra = pytest.mark.skipif(
    importlib.util.find_spec("meta_sam_parser") is None,
    reason="needs the optional linc-nerve[grounding] extra",
)

# 20x20 1-bit SAM 3.1 mask with a filled 10x10 block in the lower-right quadrant
# (rows 10..19, cols 10..19), encoded in the canonical base85 range-coded format
# accepted by meta_sam_parser. Box spans [0, 0, 19, 19] so the box centre is
# (9.5, 9.5) (< 0.5 * dim), while the mask centre-of-mass is (14.5, 14.5) (> 0.5 * dim).
LOWER_RIGHT_QUADRANT_MASK_PAYLOAD = "!!!!!)A9-nccUw("
OFF_CENTRE_SAM31_WIRE = (
    "<0f>0<|box;x1=0;y1=0;x2=19;y2=19;w=20;h=20;c=0.95|>"
    f"<|mask;x=0;y=0;c=0.95;data=20,20,{LOWER_RIGHT_QUADRANT_MASK_PAYLOAD}|>"
)


def _labelled_clickable(idx: int, field: str = "text") -> dict[str, Any]:
    return {
        "class": "android.widget.Button",
        "clickable": True,
        field: f"btn_{idx}",
        "bounds": f"[0,{idx * 10}][100,{idx * 10 + 8}]",
    }


def _unlabelled_clickable(idx: int) -> dict[str, Any]:
    return {
        "class": "android.view.View",
        "clickable": True,
        "text": "",
        "desc": "",
        "id": "",
        "resource_id": "",
        "bounds": f"[0,{idx * 10}][100,{idx * 10 + 8}]",
    }


@pytest.fixture
def grounding_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable NERVE_GROUNDING=1 for hint evaluation."""
    monkeypatch.setenv("NERVE_GROUNDING", "1")


def test_should_suggest_visual_grounding_ratio_threshold(
    monkeypatch: pytest.MonkeyPatch,
    grounding_opt_in: None,
) -> None:
    """[test] 10 clickables with 1 unlabelled -> False; 10 with 6 unlabelled -> True."""
    assert grounding._UNLABELLED_HINT_RATIO == 0.5

    ten_with_one_unlabelled = [_labelled_clickable(i) for i in range(9)] + [
        _unlabelled_clickable(9)
    ]
    assert grounding.should_suggest_visual_grounding(ten_with_one_unlabelled) is False
    assert "visual_grounding_hint" not in grounding.attach_visual_grounding_hint(
        {}, ten_with_one_unlabelled
    )

    ten_with_six_unlabelled = [_labelled_clickable(i) for i in range(4)] + [
        _unlabelled_clickable(i) for i in range(4, 10)
    ]
    assert grounding.should_suggest_visual_grounding(ten_with_six_unlabelled) is True
    hinted = grounding.attach_visual_grounding_hint({}, ten_with_six_unlabelled)
    assert hinted["visual_grounding_hint"] == grounding.VISUAL_GROUNDING_HINT

    # Wiring site check via nerve_elements
    fake_ctrl = MagicMock()
    monkeypatch.setattr(_core, "_ensure_initialized", lambda: (fake_ctrl, fake_ctrl))
    monkeypatch.setattr(_core, "_resolve_device", lambda dev: ({"serial": dev}, "android"))
    monkeypatch.setattr(_core, "_check_guard", lambda dev: None)
    monkeypatch.setattr(_core, "_get_controller", lambda platform: fake_ctrl)

    fake_ctrl.elements.return_value = ten_with_one_unlabelled
    assert "visual_grounding_hint" not in ui_tools._nerve_elements_sync(device="SER123")

    fake_ctrl.elements.return_value = ten_with_six_unlabelled
    elem_hinted = ui_tools._nerve_elements_sync(device="SER123")
    assert elem_hinted["visual_grounding_hint"] == grounding.VISUAL_GROUNDING_HINT


@pytest.mark.parametrize(
    ("unlabelled_count", "expected"),
    [
        pytest.param(0, False, id="0_of_10_unlabelled"),
        pytest.param(1, False, id="1_of_10_unlabelled"),
        pytest.param(4, False, id="4_of_10_unlabelled_below_half"),
        pytest.param(5, True, id="5_of_10_unlabelled_at_half"),
        pytest.param(6, True, id="6_of_10_unlabelled_above_half"),
        pytest.param(10, True, id="10_of_10_unlabelled"),
    ],
)
def test_should_suggest_visual_grounding_ratio_boundaries(
    grounding_opt_in: None,
    unlabelled_count: int,
    expected: bool,
) -> None:
    """[test] Boundary checks around _UNLABELLED_HINT_RATIO == 0.5."""
    labelled_count = 10 - unlabelled_count
    elements = [_labelled_clickable(i) for i in range(labelled_count)] + [
        _unlabelled_clickable(i) for i in range(labelled_count, 10)
    ]
    assert grounding.should_suggest_visual_grounding(elements) is expected


def test_should_suggest_visual_grounding_empty_error_and_zero_clickable_pins(
    grounding_opt_in: None,
) -> None:
    """[test] Pin: empty list / elements_error -> True; 0 clickable -> False."""
    assert grounding.should_suggest_visual_grounding([]) is True
    assert grounding.should_suggest_visual_grounding(None) is True
    assert (
        grounding.should_suggest_visual_grounding(
            [_labelled_clickable(0)], elements_error="dump timeout"
        )
        is True
    )

    zero_clickable = [
        {"class": "android.widget.TextView", "text": "Title", "clickable": False},
        {"class": "android.view.View", "bounds": "[0,0][100,100]"},
    ]
    assert grounding.should_suggest_visual_grounding(zero_clickable) is False


@pytest.mark.parametrize(
    "label_field",
    [
        pytest.param("text", id="labelled_by_text"),
        pytest.param("desc", id="labelled_by_desc"),
        pytest.param("id", id="labelled_by_id"),
        pytest.param("resource_id", id="labelled_by_resource_id"),
    ],
)
def test_should_suggest_visual_grounding_recognizes_all_label_fields(
    grounding_opt_in: None,
    label_field: str,
) -> None:
    """Any of text, desc, id, or resource_id marks a clickable node as labelled."""
    elements = [
        _labelled_clickable(0, field=label_field),
        _labelled_clickable(1, field=label_field),
    ]
    assert grounding.should_suggest_visual_grounding(elements) is False


def test_contract_malformed_elements_and_clickable_values_do_not_raise(
    grounding_opt_in: None,
) -> None:
    """[contract] Non-dict entries and clickable values like 'true'/None do not raise."""
    mixed: list[Any] = [
        None,
        "not-a-dict",
        42,
        ["nested"],
        {"clickable": None, "text": ""},
        {"clickable": False, "text": ""},
        {"clickable": 0, "text": ""},
        {"clickable": "true", "text": " Labelled 1 "},
        {"clickable": "true", "desc": "Labelled 2"},
        {"clickable": True, "id": "labelled_3"},
        {"clickable": True, "resource_id": "com.app:id/labelled_4"},
        {"clickable": "true", "text": "", "desc": None, "id": None},
    ]
    # 5 truthy clickables (4 labelled, 1 unlabelled) -> ratio 0.2 < 0.5 -> False without raising
    assert grounding.should_suggest_visual_grounding(mixed) is False
    payload = grounding.attach_visual_grounding_hint({"elements": mixed}, mixed)
    assert "visual_grounding_hint" not in payload

    # Non-dict entries only -> 0 clickables -> False without division by zero
    only_non_dicts: list[Any] = [None, "str", 123, {"clickable": None}]
    assert grounding.should_suggest_visual_grounding(only_non_dicts) is False


def test_nerve_state_hint_follows_ratio_threshold(
    monkeypatch: pytest.MonkeyPatch,
    grounding_opt_in: None,
) -> None:
    """[test] nerve_state wiring: hint absent at 1/10 unlabelled, present at 6/10."""
    fake_ctrl = MagicMock()
    fake_ctrl.screenshot.side_effect = RuntimeError("no screenshot in unit test")
    monkeypatch.setattr(_core, "_ensure_initialized", lambda: (fake_ctrl, fake_ctrl))
    monkeypatch.setattr(_core, "_resolve_device", lambda dev: ({"serial": dev}, "android"))
    monkeypatch.setattr(_core, "_check_guard", lambda dev: None)

    fake_ctrl.elements.return_value = [_labelled_clickable(i) for i in range(9)] + [
        _unlabelled_clickable(9)
    ]
    state = ui_tools._nerve_state_sync(device="SER123")
    assert state["element_count"] == 10
    assert "visual_grounding_hint" not in state

    fake_ctrl.elements.return_value = [_labelled_clickable(i) for i in range(4)] + [
        _unlabelled_clickable(i) for i in range(4, 10)
    ]
    state = ui_tools._nerve_state_sync(device="SER123")
    assert state["visual_grounding_hint"] == grounding.VISUAL_GROUNDING_HINT


@needs_grounding_extra
def test_pin_off_centre_mask_centroid_lands_in_lower_right_quadrant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] Pin: off-centre mask fixture computes tap centre x > 0.5*width and y > 0.5*height."""
    off_centre_mask_wire = OFF_CENTRE_SAM31_WIRE
    screen_w, screen_h = 1080, 2340
    screen_size = {"width": screen_w, "height": screen_h}

    targets = grounding.parse_sam31_records(
        off_centre_mask_wire,
        prompt="corner button",
        img_size=(20, 20),
        screen_size=screen_size,
    )
    assert len(targets) == 1
    center = targets[0]["center"]
    assert center["x"] > 0.5 * screen_w
    assert center["y"] > 0.5 * screen_h
    assert center == {
        "x": round(14.5 * (screen_w / 20)),
        "y": round(14.5 * (screen_h / 20)),
    }

    # Also verify nerve_tap(prompt=...) taps the off-centre mask centroid in device space
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), color=(30, 40, 50)).save(buf, format="PNG")
    png_bytes = buf.getvalue()

    fake_ctrl = MagicMock()
    fake_ctrl.screenshot.return_value = png_bytes
    fake_ctrl.get_screen_size.return_value = screen_size
    tapped: list[tuple[str, int, int]] = []
    fake_ctrl.tap.side_effect = lambda dev, x, y: tapped.append((dev, x, y))

    monkeypatch.setattr(_core, "_ensure_initialized", lambda: (fake_ctrl, fake_ctrl))
    monkeypatch.setattr(_core, "_resolve_device", lambda dev: ({"serial": dev}, "android"))
    monkeypatch.setattr(_core, "_check_guard", lambda dev: None)
    monkeypatch.setattr(_core, "_get_controller", lambda platform: fake_ctrl)
    monkeypatch.setattr(
        grounding, "_call_sam31_api", lambda png, prompt, key, **kw: off_centre_mask_wire
    )
    monkeypatch.setenv("META_AI_API_KEY", "test-meta-key")
    monkeypatch.setenv("NERVE_GROUNDING", "1")
    _core._LAST_GROUNDED_TAP.clear()

    tap_res = input_tools._nerve_tap_sync(device="SER123", prompt="corner button")
    assert tap_res["success"] is True
    assert tap_res["x"] > 0.5 * screen_w
    assert tap_res["y"] > 0.5 * screen_h
    assert tapped == [("SER123", center["x"], center["y"])]
