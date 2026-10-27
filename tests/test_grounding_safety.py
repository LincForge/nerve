"""[contract] Safety and licensing contracts for SAM 3.1 visual grounding.

`meta-sam-parser` ships under Meta's SAM License (field-of-use restrictions), so
it is an optional extra (`linc-nerve[grounding]`) and grounding is opt-in via
NERVE_GROUNDING=1: screenshots leave the machine only when the operator asks.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from linc_nerve.core import grounding
from linc_nerve.mcp import _core, input_tools, ui_tools

needs_parser = pytest.mark.skipif(
    importlib.util.find_spec("meta_sam_parser") is None,
    reason="needs the optional linc-nerve[grounding] extra",
)


def _png(width: int = 496, height: int = 1080) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(24, 32, 48)).save(buf, format="PNG")
    return buf.getvalue()


def _wire(*objs: tuple[int, int, int, int, float]) -> str:
    """Build SAM 3.1 wire text for boxes ``(x1, y1, x2, y2, confidence)``."""
    parts = []
    for idx, (x1, y1, x2, y2, conf) in enumerate(objs):
        sep = "<0f>0" if idx == 0 else f",{idx}"
        parts.append(
            f"{sep}<|box;x1={x1};y1={y1};x2={x2};y2={y2};w=496;h=1080;c={conf}|>"
            f"<|mask;x=0;y=0;c={conf};data={y2 - y1 + 1},{x2 - x1 + 1},!0|>"
        )
    return "".join(parts)


@pytest.fixture
def fake_device(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    ctrl = MagicMock()
    ctrl.screenshot.return_value = _png()
    ctrl.get_screen_size.return_value = {"width": 1080, "height": 2340}
    tapped: list[tuple[str, int, int]] = []
    ctrl.tap.side_effect = lambda dev, x, y: tapped.append((dev, x, y))
    uploads: list[str] = []
    state: dict[str, Any] = {"ctrl": ctrl, "tapped": tapped, "uploads": uploads, "wire": ""}

    def fake_api(png: bytes, prompt: str, key: str, **kw: Any) -> str:
        uploads.append(prompt)
        return state["wire"]

    monkeypatch.setattr(_core, "_ensure_initialized", lambda: (ctrl, ctrl))
    monkeypatch.setattr(_core, "_resolve_device", lambda dev: ({"serial": dev}, "android"))
    monkeypatch.setattr(_core, "_check_guard", lambda dev: None)
    monkeypatch.setattr(_core, "_get_controller", lambda platform: ctrl)
    monkeypatch.setattr(grounding, "_call_sam31_api", fake_api)
    monkeypatch.setenv("META_AI_API_KEY", "test-meta-key")
    monkeypatch.setenv("NERVE_GROUNDING", "1")
    _core._LAST_GROUNDED_TAP.clear()
    return state


def test_contract_server_imports_without_the_sam_parser() -> None:
    """[contract] Core nerve (all MCP tools) imports when meta-sam-parser is absent."""
    code = (
        "import sys; sys.modules['meta_sam_parser'] = None\n"
        "import linc_nerve.mcp.server, linc_nerve.mcp.ui_tools, linc_nerve.mcp.input_tools\n"
        "from linc_nerve.core import grounding\n"
        "try:\n"
        "    grounding.parse_sam31_records('<0f>0', 'x', (10, 10))\n"
        "except grounding.GroundingUnavailable as exc:\n"
        "    assert 'linc-nerve[grounding]' in str(exc), exc\n"
        "    print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == "OK"


def test_contract_sam_parser_is_not_a_core_dependency() -> None:
    """[contract] pyproject keeps meta-sam-parser out of core deps, pinned below 0.2."""
    import tomllib
    from pathlib import Path

    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())[
        "project"
    ]
    assert not any(d.startswith("meta-sam-parser") for d in project["dependencies"])
    assert project["optional-dependencies"]["grounding"] == ["meta-sam-parser>=0.1.0,<0.2"]


@pytest.mark.parametrize("value", ["", "0", "false", "no", " 1 x", "ON\n"])
def test_contract_grounding_disabled_never_uploads_or_hints(
    fake_device: dict[str, Any], monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """[contract] Without NERVE_GROUNDING=1: no screenshot upload, no tap, no hint."""
    monkeypatch.setenv("NERVE_GROUNDING", value)
    fake_device["wire"] = _wire((100, 200, 196, 300, 0.95))

    ground = ui_tools._nerve_ground_sync(device="SER", prompt="submit icon")
    tap = input_tools._nerve_tap_sync(device="SER", prompt="submit icon")

    assert "NERVE_GROUNDING=1" in ground["error"]
    assert "error" in tap
    assert fake_device["uploads"] == []
    assert fake_device["tapped"] == []
    fake_device["ctrl"].screenshot.assert_not_called()
    payload = grounding.attach_visual_grounding_hint({}, [])
    assert "visual_grounding_hint" not in payload


def test_contract_grounding_enabled_keeps_hint(fake_device: dict[str, Any]) -> None:
    assert "visual_grounding_hint" in grounding.attach_visual_grounding_hint({}, [])


@needs_parser
def test_contract_missing_key_fails_before_screenshot(
    fake_device: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[contract] A missing key is reported without capturing or uploading the screen."""
    monkeypatch.delenv("META_AI_API_KEY", raising=False)
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.setattr(grounding, "_keychain_secret", lambda account, service=None: None)

    res = ui_tools._nerve_ground_sync(device="SER", prompt="submit icon")

    assert "META_AI_API_KEY" in res["error"]
    assert "linc-env" not in res["error"]
    fake_device["ctrl"].screenshot.assert_not_called()
    assert fake_device["uploads"] == []


@needs_parser
@pytest.mark.parametrize(
    "objs",
    [
        [(100, 200, 196, 300, 0.30)],  # below the tap threshold
        [(100, 200, 196, 300, 0.91), (300, 600, 380, 700, 0.88)],  # ambiguous pair
    ],
)
def test_contract_prompt_tap_refuses_weak_or_ambiguous_match(
    fake_device: dict[str, Any], objs: list[tuple[int, int, int, int, float]]
) -> None:
    """[contract] A weak or ambiguous grounding returns candidates and taps nothing."""
    fake_device["wire"] = _wire(*objs)

    res = input_tools._nerve_tap_sync(device="SER", prompt="submit icon")

    assert "error" in res
    assert len(res["candidates"]) == len(objs)
    assert fake_device["tapped"] == []
    assert _core._LAST_GROUNDED_TAP == {}


@needs_parser
def test_contract_prompt_tap_taps_a_clear_match(fake_device: dict[str, Any]) -> None:
    fake_device["wire"] = _wire((100, 200, 196, 300, 0.92), (300, 600, 380, 700, 0.40))

    res = input_tools._nerve_tap_sync(device="SER", prompt="submit icon")

    assert res["success"] is True
    assert fake_device["tapped"] == [("SER", round(148 * 1080 / 496), round(250 * 2340 / 1080))]
    assert res["grounded"]["confidence"] == 0.92


@needs_parser
@pytest.mark.parametrize("size", [None, {}, {"width": 0, "height": 0}, {"width": -5, "height": 9}])
def test_contract_prompt_tap_refuses_unknown_screen_size(
    fake_device: dict[str, Any], size: Any
) -> None:
    """[contract] Without a real screen size the tap would land in screenshot pixels."""
    fake_device["ctrl"].get_screen_size.return_value = size
    fake_device["wire"] = _wire((100, 200, 196, 300, 0.95))

    res = input_tools._nerve_tap_sync(device="SER", prompt="submit icon")

    assert "screen size" in res["error"]
    assert fake_device["tapped"] == []


@pytest.mark.parametrize(
    "wire",
    [
        "<0f>0<|mask;x=0;y=0;c=0.9;data=100000,100000,!0|>",
        _wire((100, 200, 196, 300, 0.9)).replace("data=101,97", "data=5000,5000"),
        "<0f>0" + "x" * (grounding.MAX_SAM_RESPONSE_CHARS + 1),
    ],
)
def test_contract_oversized_sam_response_is_rejected_before_decode(
    monkeypatch: pytest.MonkeyPatch, wire: str
) -> None:
    """[contract] A tiny header claiming a huge mask cannot allocate or hang the device."""
    monkeypatch.setattr(
        grounding, "_sam_parser", lambda: (_ for _ in ()).throw(AssertionError("parsed"))
    )
    with pytest.raises(ValueError, match=r"SAM 3\.1 response"):
        grounding.parse_sam31_records(wire, "icon", (496, 1080))
