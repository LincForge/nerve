"""Unit and contract tests for SAM 3.1 visual UI grounding (`linc_nerve.core.grounding`)."""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from linc_nerve.core import grounding
from linc_nerve.mcp import _core, input_tools, ui_tools


def _make_png(width: int = 496, height: int = 1080) -> bytes:
    img = Image.new("RGB", (width, height), color=(24, 32, 48))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# Authentic SAM 3.1 wire output shape captured against `POST https://api.meta.ai/v1/responses`
LIVE_SAM31_WIRE = (
    "<0f>0<|box;x1=29;y1=534;x2=133;y2=593;w=496;h=1080;c=0.8439024686813354|>"
    "<|mask;x=0;y=0;c=0.8439024686813354;data=60,105,!0|>"
    ",1<|box;x1=200;y1=800;x2=300;y2=900;w=496;h=1080;c=0.9500|>"
    "<|mask;x=0;y=0;c=0.9500;data=101,101,!0|>"
)


def test_contract_parse_sam31_records_scales_phone_and_watch_coordinates() -> None:
    """SAM 3.1 source-pixel boxes/masks scale to phone (1080x2340) and watch (432x432) coords."""
    # 1. Phone: Stage-1 screenshot 496x1080 -> physical 1080x2340
    phone_targets = grounding.parse_sam31_records(
        LIVE_SAM31_WIRE,
        prompt="submit button",
        img_size=(496, 1080),
        screen_size={"width": 1080, "height": 2340},
    )
    assert len(phone_targets) == 2
    # Sorted descending by confidence: object 1 (0.95) before object 0 (0.8439)
    assert phone_targets[0]["object_id"] == "1"
    assert phone_targets[0]["confidence"] == pytest.approx(0.95, abs=1e-4)
    assert phone_targets[0]["center"] == {
        "x": round(250.0 * (1080 / 496)),
        "y": round(850.0 * (2340 / 1080)),
    }
    assert phone_targets[1]["object_id"] == "0"
    assert phone_targets[1]["confidence"] == pytest.approx(0.8439, abs=1e-4)
    assert phone_targets[1]["source"] == "sam-3.1"
    assert phone_targets[1]["clickable"] is True

    # 2. Wear OS Watch (432x432 -> 432x432, scale = 1.0x)
    watch_wire = (
        "<0f>0<|box;x1=180;y1=320;x2=252;y2=380;w=432;h=432;c=0.91|>"
        "<|mask;x=0;y=0;c=0.91;data=61,73,!0|>"
    )
    watch_targets = grounding.parse_sam31_records(
        watch_wire,
        prompt="ping button",
        img_size=(432, 432),
        screen_size={"width": 432, "height": 432},
    )
    assert len(watch_targets) == 1
    assert watch_targets[0]["bounds"] == "[180,320][252,380]"
    assert watch_targets[0]["center"] == {"x": 216, "y": 350}


def test_parse_sam31_records_handles_empty_and_hostile_inputs() -> None:
    """Zero matches or empty wire responses return `[]` without raising."""
    assert grounding.parse_sam31_records("", "button", (496, 1080)) == []
    assert grounding.parse_sam31_records("   ", "button", (496, 1080)) == []
    # Guard zero/missing screen_size (e.g., iOS simulator with no active WDA session)
    targets = grounding.parse_sam31_records(
        "<0f>0<|box;x1=10;y1=20;x2=30;y2=40;w=400;h=800;c=0.75|><|mask;x=0;y=0;c=0.75;data=21,21,!0|>",
        "icon",
        (400, 800),
        screen_size={"width": 0, "height": 0},
    )
    assert len(targets) == 1
    assert targets[0]["center"] == {"x": 20, "y": 30}


def test_ground_visual_targets_and_overlay_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ground_visual_targets` calls SAM 3.1 and `render_grounded_overlay` produces a valid PNG."""
    png_bytes = _make_png(496, 1080)
    monkeypatch.setattr(
        grounding,
        "_call_sam31_api",
        lambda png, prompt, key, **kw: (
            "<0f>0<|box;x1=100;y1=200;x2=200;y2=300;w=496;h=1080;c=0.88|>"
            "<|mask;x=0;y=0;c=0.88;data=101,101,!0|>"
        ),
    )
    targets = grounding.ground_visual_targets(
        png_bytes,
        "play triangle",
        screen_size={"width": 1080, "height": 2340},
        api_key="test-key",
    )
    assert len(targets) == 1
    assert targets[0]["text"] == "play triangle"

    overlay_png = grounding.render_grounded_overlay(
        png_bytes, targets, screen_size={"width": 1080, "height": 2340}
    )
    with Image.open(io.BytesIO(overlay_png)) as out_img:
        assert out_img.size == (496, 1080)


def test_should_suggest_visual_grounding() -> None:
    """Empty trees, failed dumps, or unlabelled clickable Canvas nodes trigger the hint."""
    assert grounding.should_suggest_visual_grounding([]) is True
    assert grounding.should_suggest_visual_grounding(None, elements_error="timeout") is True
    # Seeded c9 Canvas button: clickable node with empty text/desc/id
    assert (
        grounding.should_suggest_visual_grounding(
            [{"class": "android.view.View", "clickable": True, "text": "", "desc": "", "id": ""}]
        )
        is True
    )
    # Normal labelled button -> False
    assert (
        grounding.should_suggest_visual_grounding(
            [{"class": "android.widget.Button", "clickable": True, "text": "Submit"}]
        )
        is False
    )


def test_nerve_ground_and_tap_prompt_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    """`nerve_ground` and `nerve_tap(prompt=...)` resolve SAM 3.1 targets into physical taps."""
    png_bytes = _make_png(496, 1080)
    fake_ctrl = MagicMock()
    fake_ctrl.screenshot.return_value = png_bytes
    fake_ctrl.get_screen_size.return_value = {"width": 1080, "height": 2340}
    tapped: list[tuple[str, int, int]] = []
    fake_ctrl.tap.side_effect = lambda dev, x, y: tapped.append((dev, x, y))

    monkeypatch.setattr(_core, "_ensure_initialized", lambda: (fake_ctrl, fake_ctrl))
    monkeypatch.setattr(_core, "_resolve_device", lambda dev: ({"serial": dev}, "android"))
    monkeypatch.setattr(_core, "_check_guard", lambda dev: None)
    monkeypatch.setattr(_core, "_get_controller", lambda platform: fake_ctrl)
    monkeypatch.setattr(
        grounding,
        "_call_sam31_api",
        lambda png, prompt, key, **kw: (
            "<0f>0<|box;x1=100;y1=200;x2=196;y2=300;w=496;h=1080;c=0.92|>"
            "<|mask;x=0;y=0;c=0.92;data=101,97,!0|>"
        ),
    )
    monkeypatch.setenv("META_AI_API_KEY", "test-meta-key")

    ground_res = ui_tools._nerve_ground_sync(device="SER123", prompt="submit icon", annotate=False)
    assert ground_res["count"] == 1
    expected_x = round(148.0 * (1080 / 496))
    expected_y = round(250.0 * (2340 / 1080))
    assert ground_res["top_target"]["center"] == {"x": expected_x, "y": expected_y}

    tap_res: dict[str, Any] = input_tools._nerve_tap_sync(device="SER123", prompt="submit icon")
    assert tap_res["success"] is True
    assert (tap_res["x"], tap_res["y"]) == (expected_x, expected_y)
    assert tapped == [("SER123", expected_x, expected_y)]
    assert tap_res["grounded"]["prompt"] == "submit icon"
