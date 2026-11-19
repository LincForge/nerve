"""[contract] Wave 4 grounded prompt-tap safety contracts (linc-nerve-rlw2).

Covers:
(a) Guarded action-log session + grounding failure returns `{error, prompt}` with
    the grounding failure text and NEVER calls `ctrl.tap()`.
(b) A resolved prompt whose `tap()` fails (exception, NotImplementedError, or
    error dict) pops the `(device, x, y)` store entry so a subsequent manual
    `nerve_tap` at the same coordinates has no `grounded` key.
(c) Resolving 200 prompts at distinct coordinates keeps `len(store) <= cap` (64),
    evicting the oldest entries.
(d) An entry older than the 60 s monotonic TTL is not attached to a later manual tap.
(e) Before tapping a grounded point, a second screenshot is compared via a
    downscaled grayscale average hash: mismatch returns
    `{"error": "Screen changed between grounding and tap; re-run the prompt", ...}`
    without tapping; an identical screenshot taps and attaches `grounded`.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from linc_nerve.core import action_log, grounding
from linc_nerve.mcp import _core, input_tools
from linc_nerve.mcp.actionlog_tools import (
    _nerve_actionlog_start_sync,
    _nerve_actionlog_stop_sync,
)

TARGET_PKG = "com.example.app"


def _pattern_png(variant: int = 0, width: int = 160, height: int = 160) -> bytes:
    """Create a deterministic patterned PNG whose 16x16 grayscale hash varies with *variant*."""
    img = Image.new("RGB", (width, height), color=(20, 24, 32))
    draw = ImageDraw.Draw(img)
    if variant == 0:
        draw.rectangle([0, 0, width // 2, height // 2], fill=(240, 240, 240))
    else:
        draw.rectangle([width // 2, height // 2, width - 1, height - 1], fill=(240, 240, 240))
        draw.rectangle([0, height * 3 // 4, width - 1, height - 1], fill=(220, 220, 220))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeTapController:
    """Fake device controller recording screenshot and tap calls without I/O."""

    def __init__(self) -> None:
        self.screenshots: list[bytes] = [_pattern_png(0)]
        self.screenshot_calls: int = 0
        self.tapped: list[tuple[str, Any, Any]] = []
        self.tap_error: BaseException | None = None
        self.foreground: str = TARGET_PKG
        self.tree: list[dict[str, Any]] = [
            {"class": "android.widget.FrameLayout", "bounds": "[0,0][1080,2340]"},
            {
                "class": "android.widget.Button",
                "bounds": "[100,100][400,400]",
                "text": "Submit",
                "clickable": True,
            },
        ]

    def get_screen_size(self, serial: str) -> dict[str, int]:
        return {"width": 1080, "height": 2340}

    def screenshot(self, serial: str) -> bytes:
        idx = min(self.screenshot_calls, len(self.screenshots) - 1)
        self.screenshot_calls += 1
        return self.screenshots[idx]

    def tap(self, serial: str, x: Any, y: Any) -> None:
        if self.tap_error is not None:
            err = self.tap_error
            self.tap_error = None
            raise err
        self.tapped.append((serial, x, y))

    def get_foreground(self, serial: str) -> dict[str, str]:
        return {"package": self.foreground, "activity": ".Main"}

    def elements(self, serial: str, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.tree)


def _target(x: int = 200, y: int = 300, confidence: float = 0.95) -> list[dict[str, Any]]:
    return [
        {
            "text": "submit button",
            "desc": "submit button",
            "class": "VisualTarget",
            "bounds": f"[{x - 20},{y - 20}][{x + 20},{y + 20}]",
            "center": {"x": x, "y": y},
            "confidence": confidence,
            "object_id": "0",
            "clickable": True,
            "source": "sam-3.1",
        }
    ]


@pytest.fixture
def fake_ctrl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeTapController:
    ctrl = FakeTapController()
    action_log.REGISTRY.reset()
    _core._LAST_GROUNDED_TAP.clear()
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(tmp_path))
    monkeypatch.setenv("NERVE_GROUNDING", "1")
    monkeypatch.setenv("META_AI_API_KEY", "test-meta-key")
    monkeypatch.setattr(_core, "_ensure_initialized", lambda: (ctrl, ctrl))
    monkeypatch.setattr(_core, "_resolve_device", lambda dev: ({"serial": dev}, "android"))
    monkeypatch.setattr(_core, "_check_guard", lambda dev: None)
    monkeypatch.setattr(_core, "_get_controller", lambda platform: ctrl)
    yield ctrl
    action_log.REGISTRY.reset()
    _core._LAST_GROUNDED_TAP.clear()


def test_contract_a_guarded_session_grounding_failure_returns_error_and_never_taps(
    fake_ctrl: FakeTapController,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """[contract] (a) Guarded session + grounding raising returns error and never taps."""
    log_path = tmp_path / "guarded.jsonl"
    started = _nerve_actionlog_start_sync("S", str(log_path), target_package=TARGET_PKG)
    assert started["success"] is True

    def raising_ground(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("SAM 3.1 upstream timeout")

    monkeypatch.setattr(grounding, "ground_visual_targets", raising_ground)

    canon = _core._canonical_tap_args({"device": "S", "prompt": "submit button"})
    assert canon == {
        "error": "Visual grounding failed: RuntimeError: SAM 3.1 upstream timeout",
        "prompt": "submit button",
    }

    res = input_tools._nerve_tap_sync("S", prompt="submit button")
    _nerve_actionlog_stop_sync("S")

    assert res == {
        "error": "Visual grounding failed: RuntimeError: SAM 3.1 upstream timeout",
        "prompt": "submit button",
    }
    assert fake_ctrl.tapped == []

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    guard_lines = [r for r in records if r.get("type") == "guard"]
    assert len(guard_lines) == 2
    assert guard_lines[0]["tool"] == "nerve_tap"
    assert guard_lines[0]["reason"] == "target_unreadable"
    assert "SAM 3.1 upstream timeout" in guard_lines[0]["detail"]


def test_contract_b_failed_prompt_tap_drops_store_entry_and_manual_tap_has_no_grounded(
    fake_ctrl: FakeTapController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] (b) A resolved prompt whose tap() raises leaves no store entry for manual tap."""
    monkeypatch.setattr(
        grounding,
        "ground_visual_targets",
        lambda png, prompt, **kw: _target(200, 300),
    )
    fake_ctrl.tap_error = RuntimeError("adb input socket closed")

    failed = input_tools._nerve_tap_sync("S", prompt="submit button")
    assert failed == {"error": "Tap failed: adb input socket closed"}
    assert len(_core._LAST_GROUNDED_TAP) == 0
    assert _core._LAST_GROUNDED_TAP._timestamps == {}
    assert _core._LAST_GROUNDED_TAP._screen_hashes == {}

    manual = input_tools._nerve_tap_sync("S", 200, 300)
    assert manual == {"success": True, "action": "tap", "x": 200, "y": 300}
    assert "grounded" not in manual

    # NotImplementedError outcome also pops the entry
    fake_ctrl.tap_error = NotImplementedError("tap not supported on watch")
    not_impl = input_tools._nerve_tap_sync("S", prompt="submit button")
    assert not_impl == {"error": "tap not supported on watch"}
    assert len(_core._LAST_GROUNDED_TAP) == 0

    # Error dict outcomes (_check_guard / _resolve_device) inside _nerve_tap_sync also pop
    _core._canonical_tap_args({"device": "S", "prompt": "submit button"})
    assert len(_core._LAST_GROUNDED_TAP) == 1
    monkeypatch.setattr(_core, "_check_guard", lambda dev: {"error": "Device reserved"})
    reserved = input_tools._nerve_tap_sync("S", 200, 300)
    assert reserved == {"error": "Device reserved"}
    assert len(_core._LAST_GROUNDED_TAP) == 0


def test_contract_c_resolving_200_prompts_keeps_store_within_cap(
    fake_ctrl: FakeTapController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] (c) Resolving 200 prompts at distinct points leaves len(store) <= cap (64)."""
    for idx in range(200):
        x, y = 10 + idx, 100 + idx
        monkeypatch.setattr(
            grounding,
            "ground_visual_targets",
            lambda png, prompt, _x=x, _y=y, **kw: _target(_x, _y),
        )
        bound = _core._canonical_tap_args({"device": "S", "prompt": f"item-{idx}"})
        assert bound == {"device": "S", "x": x, "y": y}

    assert grounding.GROUNDED_TAP_CAP == 64
    assert _core._LAST_GROUNDED_TAP.cap == 64
    assert len(_core._LAST_GROUNDED_TAP) <= grounding.GROUNDED_TAP_CAP
    assert len(_core._LAST_GROUNDED_TAP) == 64
    assert len(_core._LAST_GROUNDED_TAP._timestamps) == 64
    assert len(_core._LAST_GROUNDED_TAP._screen_hashes) == 64
    # Oldest entries (0..135) evicted, newest 64 (136..199) retained
    assert ("S", 10 + 135, 100 + 135) not in _core._LAST_GROUNDED_TAP
    assert ("S", 10 + 136, 100 + 136) in _core._LAST_GROUNDED_TAP
    assert ("S", 10 + 199, 100 + 199) in _core._LAST_GROUNDED_TAP
    assert "_screen_hash" not in _core._LAST_GROUNDED_TAP[("S", 10 + 199, 100 + 199)]

    # Re-inserting an existing key refreshes its position without evicting another entry
    oldest_remaining = ("S", 10 + 136, 100 + 136)
    second_oldest = ("S", 10 + 137, 100 + 137)
    _core._LAST_GROUNDED_TAP[oldest_remaining] = {"prompt": "refreshed", "_screen_hash": 42}
    assert len(_core._LAST_GROUNDED_TAP) == 64
    assert oldest_remaining in _core._LAST_GROUNDED_TAP
    assert second_oldest in _core._LAST_GROUNDED_TAP
    _core._LAST_GROUNDED_TAP[("S", 999, 999)] = {"prompt": "new", "_screen_hash": 99}
    assert second_oldest not in _core._LAST_GROUNDED_TAP
    assert oldest_remaining in _core._LAST_GROUNDED_TAP


def test_contract_d_entry_older_than_ttl_is_not_attached(
    fake_ctrl: FakeTapController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] (d) An entry older than the 60 s monotonic TTL is not attached."""
    assert grounding.GROUNDED_TAP_TTL_S == 60.0
    now = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: now)
    monkeypatch.setattr(
        grounding,
        "ground_visual_targets",
        lambda png, prompt, **kw: _target(220, 340),
    )

    # Exact TTL boundary (60.0 s) is still fresh
    _core._canonical_tap_args({"device": "S", "prompt": "submit button"})
    now = 1060.0
    at_boundary = input_tools._nerve_tap_sync("S", 220, 340)
    assert at_boundary["grounded"]["prompt"] == "submit button"

    # Past the 60 s TTL (60.001 s) is evicted and not attached
    now = 2000.0
    bound = _core._canonical_tap_args({"device": "S", "prompt": "submit button"})
    assert bound == {"device": "S", "x": 220, "y": 340}
    assert ("S", 220, 340) in _core._LAST_GROUNDED_TAP

    now = 2060.001
    res = input_tools._nerve_tap_sync("S", 220, 340)
    assert res == {"success": True, "action": "tap", "x": 220, "y": 340}
    assert "grounded" not in res
    assert len(_core._LAST_GROUNDED_TAP) == 0
    assert _core._LAST_GROUNDED_TAP._timestamps == {}
    assert _core._LAST_GROUNDED_TAP._screen_hashes == {}


def test_contract_e_changed_screen_aborts_tap_and_identical_screen_taps_with_grounded(
    fake_ctrl: FakeTapController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] (e) Second screenshot differing -> error and no tap; identical -> tap."""
    monkeypatch.setattr(
        grounding,
        "ground_visual_targets",
        lambda png, prompt, **kw: _target(250, 400, confidence=0.93),
    )

    # 1. Screen changes between grounding screenshot and pre-tap screenshot
    fake_ctrl.screenshots = [_pattern_png(0), _pattern_png(1)]
    fake_ctrl.screenshot_calls = 0

    changed = input_tools._nerve_tap_sync("S", prompt="submit button")
    assert changed == {
        "error": "Screen changed between grounding and tap; re-run the prompt",
        "prompt": "submit button",
        "grounded": {
            "prompt": "submit button",
            "confidence": 0.93,
            "bounds": "[230,380][270,420]",
            "source": "sam-3.1",
        },
    }
    assert fake_ctrl.tapped == []
    assert len(_core._LAST_GROUNDED_TAP) == 0

    # 2. Pin of the happy path: identical second screenshot taps and attaches grounded metadata
    fake_ctrl.screenshots = [_pattern_png(0), _pattern_png(0)]
    fake_ctrl.screenshot_calls = 0

    ok = input_tools._nerve_tap_sync("S", prompt="submit button")
    assert ok == {
        "success": True,
        "action": "tap",
        "x": 250,
        "y": 400,
        "grounded": {
            "prompt": "submit button",
            "confidence": 0.93,
            "bounds": "[230,380][270,420]",
            "source": "sam-3.1",
        },
    }
    assert fake_ctrl.tapped == [("S", 250, 400)]
    assert fake_ctrl.screenshot_calls == 2


def test_screenshot_hash_and_store_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin 16x16 hash bit-width, solid-color fallback, threshold boundary, and store helpers."""
    assert grounding.SCREEN_HASH_SIZE == 16
    assert grounding.SCREEN_HASH_THRESHOLD == 5

    # Pattern 0 has top-left white (pixel 0 is above avg -> MSB 1 in 256-bit hash)
    h0 = grounding.screenshot_hash(_pattern_png(0))
    assert h0.bit_length() == 256
    assert grounding.screenshots_match(h0, _pattern_png(0)) is True
    assert grounding.screenshots_match(h0 ^ 0b11111, _pattern_png(0)) is True
    assert grounding.screenshots_match(h0 ^ 0b111111, _pattern_png(0)) is False

    # Solid-color images fall back to SHA-256 digest
    buf_a, buf_b = io.BytesIO(), io.BytesIO()
    Image.new("RGB", (32, 32), color=(10, 10, 10)).save(buf_a, format="PNG")
    Image.new("RGB", (32, 32), color=(200, 200, 200)).save(buf_b, format="PNG")
    solid_a, solid_b = buf_a.getvalue(), buf_b.getvalue()
    assert grounding.screenshot_hash(solid_a) == int.from_bytes(
        hashlib.sha256(solid_a).digest(), "big"
    )
    assert grounding.screenshots_match(grounding.screenshot_hash(solid_a), solid_b) is False

    # Store screen_hash helper and clear
    store = grounding.GroundedTapStore(cap=4, ttl_s=10.0)
    store[("S", 1, 2)] = {"prompt": "p", "_screen_hash": 12345}
    assert store.screen_hash("S", None, 2) is None
    assert store.screen_hash("S", 1, None) is None
    assert store.screen_hash("S", 1, 2) == 12345
    assert store.pop(("S", 9, 9), "missing") == "missing"
    store.clear()
    assert len(store) == 0
    assert store._timestamps == {}
    assert store._screen_hashes == {}

    # require_grounding still checks _sam_parser when ground_visual_targets is unpatched
    monkeypatch.setenv("NERVE_GROUNDING", "1")
    monkeypatch.setenv("META_AI_API_KEY", "k")
    monkeypatch.setattr(
        grounding,
        "_sam_parser",
        lambda: (_ for _ in ()).throw(grounding.GroundingUnavailable("no parser")),
    )
    with pytest.raises(grounding.GroundingUnavailable, match="no parser"):
        grounding.require_grounding()
