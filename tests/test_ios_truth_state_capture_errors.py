"""[contract] nerve_state capture error truthfulness tests (linc-nerve-8m9g)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from linc_nerve.core import grounding
from linc_nerve.mcp import _core, ui_tools

SIM_UDID = "00000000-0000-0000-0000-000000000001"
ANDROID_SERIAL = "PIXEL8PRO"
EXPECTED_IOS_ELEMENTS_ERROR = (
    "element tree not collected by nerve_state on iOS; call nerve_elements"
)


def _make_png(width: int = 20, height: int = 40) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _wire_controllers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    platform: str,
    device: str,
    android: MagicMock | None = None,
    ios: MagicMock | None = None,
) -> tuple[MagicMock, MagicMock]:
    fake_android = android or MagicMock()
    fake_ios = ios or MagicMock()
    counter = {"n": 0}

    def _next_path(dev: str, suffix: str, ext: str = "png") -> str:
        counter["n"] += 1
        return str(tmp_path / f"{dev}_{counter['n']:04d}_{suffix}.{ext}")

    monkeypatch.setattr(_core, "_ensure_initialized", lambda: (fake_android, fake_ios))
    monkeypatch.setattr(_core, "_resolve_device", lambda dev: ({"serial": dev}, platform))
    monkeypatch.setattr(_core, "_check_guard", lambda dev: None)
    monkeypatch.setattr(_core, "_next_output_path", _next_path)
    return fake_android, fake_ios


def test_contract_ios_screenshot_failure_reports_screenshot_and_elements_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """[contract] iOS screenshot failure carries screenshot_error + elements_error."""
    monkeypatch.delenv("NERVE_GROUNDING", raising=False)
    fake_ios = MagicMock()
    fake_ios.screenshot.side_effect = RuntimeError("WDA_PROJECT_DIR not set")
    _wire_controllers(
        monkeypatch,
        tmp_path,
        platform="ios",
        device=SIM_UDID,
        ios=fake_ios,
    )

    result = ui_tools._nerve_state_sync(device=SIM_UDID)

    assert result["device"] == SIM_UDID
    assert result["platform"] == "ios"
    assert result["screenshot_error"] == "WDA_PROJECT_DIR not set"
    assert result["elements_error"] == EXPECTED_IOS_ELEMENTS_ERROR
    assert "screenshot" not in result
    assert "elements" not in result
    assert "element_count" not in result
    fake_ios.elements.assert_not_called()


def test_contract_android_screenshot_failure_keeps_elements_and_reports_screenshot_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """[contract] Android screenshot failure sets screenshot_error while keeping elements."""
    monkeypatch.delenv("NERVE_GROUNDING", raising=False)
    fake_android = MagicMock()
    fake_android.get_foreground.return_value = {"package": "com.example.app", "activity": ".Main"}
    fake_android.get_orientation.return_value = "portrait"
    fake_android.get_screen_size.return_value = {"width": 1080, "height": 2400}
    fake_android.elements.return_value = [
        {"text": "Continue", "clickable": True, "bounds": "[0,0][100,50]"}
    ]
    fake_android.screenshot.side_effect = RuntimeError("screencap timed out after 15s")
    _wire_controllers(
        monkeypatch,
        tmp_path,
        platform="android",
        device=ANDROID_SERIAL,
        android=fake_android,
    )

    result = ui_tools._nerve_state_sync(device=ANDROID_SERIAL)

    assert result["device"] == ANDROID_SERIAL
    assert result["platform"] == "android"
    assert result["screenshot_error"] == "screencap timed out after 15s"
    assert "screenshot" not in result
    assert result["elements"] == [
        {"text": "Continue", "clickable": True, "bounds": "[0,0][100,50]"}
    ]
    assert result["element_count"] == 1
    assert "elements_error" not in result


@pytest.mark.parametrize(
    ("platform", "screenshot_ok", "elements_ok"),
    [
        pytest.param("android", True, True, id="android_screenshot_ok_elements_ok"),
        pytest.param("android", True, False, id="android_screenshot_ok_elements_raises"),
        pytest.param("android", False, True, id="android_screenshot_raises_elements_ok"),
        pytest.param("android", False, False, id="android_screenshot_raises_elements_raises"),
        pytest.param("ios", True, True, id="ios_screenshot_ok_elements_ok"),
        pytest.param("ios", True, False, id="ios_screenshot_ok_elements_raises"),
        pytest.param("ios", False, True, id="ios_screenshot_raises_elements_ok"),
        pytest.param("ios", False, False, id="ios_screenshot_raises_elements_raises"),
    ],
)
def test_contract_state_error_exclusivity_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    screenshot_ok: bool,
    elements_ok: bool,
) -> None:
    """[contract] Every (platform, screenshot, elements) state satisfies XOR invariants."""
    monkeypatch.delenv("NERVE_GROUNDING", raising=False)
    png_bytes = _make_png()
    fake_android = MagicMock()
    fake_android.get_foreground.return_value = {"package": "com.example.app", "activity": ".Main"}
    fake_android.get_orientation.return_value = "portrait"
    fake_android.get_screen_size.return_value = {"width": 1080, "height": 2400}
    if screenshot_ok:
        fake_android.screenshot.return_value = png_bytes
    else:
        fake_android.screenshot.side_effect = RuntimeError("android screenshot failed")
    if elements_ok:
        fake_android.elements.return_value = [{"text": "OK", "clickable": True}]
    else:
        fake_android.elements.side_effect = RuntimeError("android elements failed")

    fake_ios = MagicMock()
    if screenshot_ok:
        fake_ios.screenshot.return_value = png_bytes
    else:
        fake_ios.screenshot.side_effect = RuntimeError("ios screenshot failed")
    if elements_ok:
        fake_ios.elements.return_value = [{"text": "OK", "clickable": True}]
    else:
        fake_ios.elements.side_effect = RuntimeError("ios elements failed")

    device = ANDROID_SERIAL if platform == "android" else SIM_UDID
    _wire_controllers(
        monkeypatch,
        tmp_path,
        platform=platform,
        device=device,
        android=fake_android,
        ios=fake_ios,
    )

    r: dict[str, Any] = ui_tools._nerve_state_sync(device=device)

    assert ("screenshot" in r) != ("screenshot_error" in r)
    assert ("elements" in r) != ("elements_error" in r)
    if platform == "ios":
        fake_ios.elements.assert_not_called()
        assert r["elements_error"] == EXPECTED_IOS_ELEMENTS_ERROR


def test_pin_all_success_android_and_ios_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """[test] Pin: all-success Android and iOS paths are unchanged except iOS elements_error."""
    monkeypatch.setenv("NERVE_GROUNDING", "1")
    png_bytes = _make_png(width=20, height=40)

    fake_android = MagicMock()
    fake_android.get_foreground.return_value = {"package": "com.example.app", "activity": ".Main"}
    fake_android.get_orientation.return_value = "portrait"
    fake_android.get_screen_size.return_value = {"width": 1080, "height": 2400}
    fake_android.elements.return_value = [
        {"text": "Home", "clickable": True, "bounds": "[0,0][100,100]"}
    ]
    fake_android.screenshot.return_value = png_bytes

    _wire_controllers(
        monkeypatch,
        tmp_path,
        platform="android",
        device=ANDROID_SERIAL,
        android=fake_android,
    )
    android_res = ui_tools._nerve_state_sync(device=ANDROID_SERIAL)
    assert android_res["device"] == ANDROID_SERIAL
    assert android_res["platform"] == "android"
    assert android_res["foreground"] == {"package": "com.example.app", "activity": ".Main"}
    assert android_res["orientation"] == "portrait"
    assert android_res["screen_size"] == {"width": 1080, "height": 2400}
    assert android_res["elements"] == [
        {"text": "Home", "clickable": True, "bounds": "[0,0][100,100]"}
    ]
    assert android_res["element_count"] == 1
    assert android_res["screenshot_size"] == len(png_bytes)
    assert android_res["screenshot"].endswith("_state.png")
    assert "screenshot_error" not in android_res
    assert "elements_error" not in android_res
    assert "visual_grounding_hint" not in android_res

    fake_ios = MagicMock()
    fake_ios.screenshot.return_value = png_bytes
    _wire_controllers(
        monkeypatch,
        tmp_path,
        platform="ios",
        device=SIM_UDID,
        ios=fake_ios,
    )
    ios_res = ui_tools._nerve_state_sync(device=SIM_UDID)
    assert ios_res["device"] == SIM_UDID
    assert ios_res["platform"] == "ios"
    assert ios_res["screenshot_size"] == len(png_bytes)
    assert ios_res["screenshot"].endswith("_state.png")
    assert ios_res["screenshot_width"] == 20
    assert ios_res["screenshot_height"] == 40
    assert ios_res["elements_error"] == EXPECTED_IOS_ELEMENTS_ERROR
    assert ios_res["visual_grounding_hint"] == grounding.VISUAL_GROUNDING_HINT
    assert "screenshot_error" not in ios_res
    assert "elements" not in ios_res
    assert "element_count" not in ios_res
    fake_ios.elements.assert_not_called()


def test_long_exception_message_is_truncated_to_300_chars(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """[test] A 5000-char exception message is truncated to 300 chars, never echoed whole."""
    monkeypatch.delenv("NERVE_GROUNDING", raising=False)
    long_msg = "E" * 5000

    fake_ios = MagicMock()
    fake_ios.screenshot.side_effect = RuntimeError(long_msg)
    _wire_controllers(
        monkeypatch,
        tmp_path,
        platform="ios",
        device=SIM_UDID,
        ios=fake_ios,
    )
    ios_res = ui_tools._nerve_state_sync(device=SIM_UDID)
    assert len(ios_res["screenshot_error"]) == 300
    assert ios_res["screenshot_error"] == "E" * 300

    fake_android = MagicMock()
    fake_android.get_foreground.return_value = {}
    fake_android.get_orientation.return_value = "portrait"
    fake_android.get_screen_size.return_value = {}
    fake_android.elements.side_effect = RuntimeError(long_msg)
    fake_android.screenshot.side_effect = RuntimeError(long_msg)
    _wire_controllers(
        monkeypatch,
        tmp_path,
        platform="android",
        device=ANDROID_SERIAL,
        android=fake_android,
    )
    android_res = ui_tools._nerve_state_sync(device=ANDROID_SERIAL)
    assert len(android_res["screenshot_error"]) == 300
    assert android_res["screenshot_error"] == "E" * 300
    assert len(android_res["elements_error"]) == 300
    assert android_res["elements_error"] == "E" * 300


def test_nerve_state_docstring_mentions_screenshot_error_and_elements_error() -> None:
    """[test] nerve_state Returns docstring documents both screenshot_error and elements_error."""
    doc = ui_tools._nerve_state_sync.__doc__ or ""
    assert "screenshot_error" in doc
    assert "elements_error" in doc
