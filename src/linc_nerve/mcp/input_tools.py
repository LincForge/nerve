"""Input tools — tap, long press, swipe, type, key."""

from __future__ import annotations

from typing import Any

from linc_nerve.core import grounding
from linc_nerve.core.grounding import attach_grounded_tap
from linc_nerve.core.state_engine import validate_idle_timeout_ms
from linc_nerve.mcp import _core
from linc_nerve.mcp._core import device_tool
from linc_nerve.mcp._stability import _check_pil_support, _poll_stability

MAX_GESTURE_DURATION_MS = 60_000


def _validate_wait_idle_ms(wait_idle_ms: Any) -> dict[str, Any] | None:
    """Return a validation_error dict when *wait_idle_ms* is out of 0..10000, else None."""
    try:
        validate_idle_timeout_ms(wait_idle_ms, name="wait_idle_ms")
    except ValueError as exc:
        return {"error": str(exc), "validation_error": True}
    return None


def _validate_duration_ms(duration_ms: Any) -> dict[str, Any] | None:
    """Return a validation_error dict when *duration_ms* is not an int in 0..60000."""
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or duration_ms < 0
        or duration_ms > MAX_GESTURE_DURATION_MS
    ):
        return {
            "error": (
                f"duration_ms must be an int in 0..{MAX_GESTURE_DURATION_MS}, got {duration_ms!r}"
            ),
            "validation_error": True,
        }
    return None


def _validate_gesture_args(wait_idle_ms: Any, duration_ms: Any) -> dict[str, Any] | None:
    """Validate *wait_idle_ms* and *duration_ms* before resolving a device or calling adb."""
    return _validate_wait_idle_ms(wait_idle_ms) or _validate_duration_ms(duration_ms)


def _resolve_and_guard(device: str) -> tuple[Any, str, dict[str, Any] | None]:
    """Resolve *device* and check reservation guard; return (ctrl, platform, error_or_none)."""
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return None, platform, {"error": platform}
    guard = _core._check_guard(device)
    if guard:
        return None, platform, guard
    return _core._get_controller(platform), platform, None


def _with_idle(
    result: dict[str, Any],
    ctrl: Any,
    device: str,
    platform: str,
    wait_idle_ms: int,
) -> dict[str, Any]:
    """Wait for UI to settle when *wait_idle_ms* > 0 and attach ``idle`` report."""
    if wait_idle_ms <= 0 or "error" in result:
        return result
    use_pil = _check_pil_support(ctrl, device, platform)
    stability = _poll_stability(ctrl, device, wait_idle_ms, use_pil)
    return {
        **result,
        "idle": {
            "stable": stability["stable"],
            "elapsed_ms": stability["elapsed_ms"],
            "polls": stability["polls"],
            "reason": stability["reason"],
        },
    }


@device_tool
def _nerve_tap_sync(
    device: str,
    x: int | None = None,
    y: int | None = None,
    prompt: str = "",
    wait_idle_ms: int = 0,
) -> dict[str, Any]:
    """Tap a single point on the device display at known x/y coordinates or by visual prompt.

    A one-shot single-touch gesture: routes a click to one specific pixel
    position. Use when an agent already knows where to tap — bounds from
    nerve_elements, an annotated screenshot, a fixed UI position, or a
    semantic visual concept prompt grounded via SAM 3.1 when the UI tree is empty.

    USE FOR: tapping a single point on the screen, clicking a coordinate,
        pressing a single position on the device display, single touch
        input at xy coordinates, clicking a UI element at a known
        position — for example "tap the login button at 540, 1200" or
        "click at the given screen position".
    NOT FOR: a two-point gesture, drag, or scroll (use nerve_swipe instead),
        entering text into a focused field (use nerve_type instead),
        pressing a hardware key like HOME/BACK/ENTER (use nerve_key instead),
        or a multi-second hold (use nerve_long_press instead).
    TIP: call nerve_elements first if you need an element's bounds, then
        tap the rect center. When the UI tree is empty or unlabelled, pass
        prompt='<visual description>' to ground via SAM 3.1. Returns ~30-80 tokens.

    Args:
        device: Device serial.
        x: X coordinate in pixels (optional when prompt is provided).
        y: Y coordinate in pixels (optional when prompt is provided).
        prompt: Visual concept description grounded via SAM 3.1 when x and y are omitted.
        wait_idle_ms: Settle wait in ms.

    Returns:
        Dict with success/action/x/y on success, or {"error": ...} on failure.
    """
    invalid_idle = _validate_wait_idle_ms(wait_idle_ms)
    if invalid_idle is not None:
        return invalid_idle

    screen_hash = _core._LAST_GROUNDED_TAP.screen_hash(device, x, y)
    res = attach_grounded_tap(
        {"success": True, "action": "tap", "x": x, "y": y},
        _core._LAST_GROUNDED_TAP,
        device,
        x,
        y,
    )

    ctrl, platform, err = _resolve_and_guard(device)
    if err is not None:
        return err

    try:
        tap_res = grounding.verified_tap(ctrl, device, x, y, res, screen_hash)
        return _with_idle(tap_res, ctrl, device, platform, wait_idle_ms)
    except NotImplementedError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Tap failed: {exc}"}


@device_tool
def _nerve_long_press_sync(
    device: str,
    x: int,
    y: int,
    duration_ms: int = 500,
    wait_idle_ms: int = 0,
) -> dict[str, Any]:
    """Long press at screen coordinates.

    USE FOR: Context menus, drag initiation, text selection, hold-to-reveal actions.
    NOT FOR: Quick taps (use nerve_tap), scrolling (use nerve_swipe).
    TIP: The hold must EXCEED the device's long_press_timeout (check with
    `adb shell settings get secure long_press_timeout`) or it resolves as a
    tap. Samsung factory defaults (500/1000ms) TIE the 500ms default — pass
    duration_ms=1200 for Samsung/unknown Android. iOS may need 1000ms+.

    Args:
        device: Device serial.
        x: X coordinate in pixels.
        y: Y coordinate in pixels.
        duration_ms: Hold duration in milliseconds; must exceed the device's
            long_press_timeout (1200 for Samsung/unknown Android, 1000+ for
            iOS). Default 500.
        wait_idle_ms: Settle wait in ms.
    """
    invalid = _validate_gesture_args(wait_idle_ms, duration_ms)
    if invalid is not None:
        return invalid

    ctrl, platform, err = _resolve_and_guard(device)
    if err is not None:
        return err

    try:
        ctrl.long_press(device, x, y, duration_ms)
        return _with_idle(
            {
                "success": True,
                "action": "long_press",
                "x": x,
                "y": y,
                "duration_ms": duration_ms,
            },
            ctrl,
            device,
            platform,
            wait_idle_ms,
        )
    except NotImplementedError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Long press failed: {exc}"}


@device_tool
def _nerve_swipe_sync(
    device: str,
    start_x: int | None = None,
    start_y: int | None = None,
    end_x: int | None = None,
    end_y: int | None = None,
    duration_ms: int = 300,
    x1: int | None = None,
    y1: int | None = None,
    x2: int | None = None,
    y2: int | None = None,
    wait_idle_ms: int = 0,
) -> dict[str, Any]:
    """Two-point gesture from one xy coordinate to another — swipe, drag, or scroll.

    USE FOR: swiping the screen, drag gestures between coordinates, scroll
        gestures (swipe up to scroll content down; swipe from the top to
        reveal the notification shade), swipe-to-dismiss.
    NOT FOR: a single-point tap (nerve_tap), text entry (nerve_type), deep
        link / URI navigation (nerve_navigate), hardware keys like BACK
        (nerve_key), a slow hold-in-place (nerve_long_press).
    TIP: default 300ms suits flicks; 800ms+ for slow drags.

    Args:
        device: Device serial.
        start_x: Start X.
        start_y: Start Y.
        end_x: End X.
        end_y: End Y.
        duration_ms: Duration in ms (default 300).
        x1: Alias for start_x.
        y1: Alias for start_y.
        x2: Alias for end_x.
        y2: Alias for end_y.
        wait_idle_ms: Settle wait in ms.

    Returns:
        Dict with success/action/from/to, or {"error": ...}.
    """
    invalid = _validate_gesture_args(wait_idle_ms, duration_ms)
    if invalid is not None:
        return invalid

    ctrl, platform, err = _resolve_and_guard(device)
    if err is not None:
        return err

    try:
        ctrl.swipe(device, start_x, start_y, end_x, end_y, duration_ms)
        return _with_idle(
            {
                "success": True,
                "action": "swipe",
                "from": [start_x, start_y],
                "to": [end_x, end_y],
            },
            ctrl,
            device,
            platform,
            wait_idle_ms,
        )
    except NotImplementedError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Swipe failed: {exc}"}


@device_tool
def _nerve_type_sync(
    device: str,
    text: str,
    secret: bool = False,
    wait_idle_ms: int = 0,
) -> dict[str, Any]:
    """Type text into the currently focused input field via the soft keyboard.

    USE FOR: typing text into a focused field, soft-keyboard input, text
        entry into a search bar / login form / active input.
    NOT FOR: tapping to focus the field first (nerve_tap), scroll/swipe/drag
        (nerve_swipe), hardware/system keys like HOME, BACK, ENTER
        (nerve_key), speaking to a council advisor (council_speak).
    TIP: tap the field first with nerve_tap; keystrokes go to whatever has
        focus. iOS may need WebDriverAgent for non-ASCII.

    Args:
        device: Device serial.
        text: Text to type.
        secret: Redact the text (report its length only) in results and logs.
        wait_idle_ms: Settle wait in ms.

    Returns:
        Dict with success/action/text on success, or {"error": ...} on failure.
    """
    invalid_idle = _validate_wait_idle_ms(wait_idle_ms)
    if invalid_idle is not None:
        return invalid_idle

    ctrl, platform, err = _resolve_and_guard(device)
    if err is not None:
        return err

    try:
        ctrl.type_text(device, text)
        return _with_idle(
            {"success": True, "action": "type", **_typed_text(text, secret)},
            ctrl,
            device,
            platform,
            wait_idle_ms,
        )
    except Exception as exc:
        return {"error": _scrub_secret(f"Type failed: {exc}", text, secret)}


def _typed_text(text: str, secret: bool) -> dict[str, Any]:
    """Result echo of typed text — a secret is reported by length only."""
    if secret:
        return {"text": "<redacted>", "length": len(text)}
    return {"text": text}


def _scrub_secret(message: str, text: str, secret: bool) -> str:
    """Keep a secret out of an error message that might quote it."""
    return message.replace(text, "<redacted>") if secret and text else message


@device_tool
def _nerve_key_sync(
    device: str,
    key: str = "",
    keycode: str = "",
    wait_idle_ms: int = 0,
) -> dict[str, Any]:
    """Press a hardware/system key.

    USE FOR: HOME, BACK, ENTER, VOLUME_UP, VOLUME_DOWN, POWER, TAB, DELETE.
    NOT FOR: Typing text (use nerve_type).
    TIP: Android supports all standard KeyEvent codes. iOS support is limited.

    Args:
        device: Device serial.
        key: Key name (HOME, BACK, ENTER, etc.).
        keycode: Alias for key (HOME, BACK, ENTER, etc.).
        wait_idle_ms: Settle wait in ms.
    """
    invalid_idle = _validate_wait_idle_ms(wait_idle_ms)
    if invalid_idle is not None:
        return invalid_idle

    ctrl, platform, err = _resolve_and_guard(device)
    if err is not None:
        return err

    try:
        ctrl.press_key(device, key)
        return _with_idle(
            {"success": True, "action": "key", "key": key},
            ctrl,
            device,
            platform,
            wait_idle_ms,
        )
    except Exception as exc:
        return {"error": f"Key press failed: {exc}"}


@device_tool
def _nerve_density_sync(device: str) -> dict[str, Any]:
    """Read display density — dpi and the dp-to-px multiplier — for a device.

    USE FOR: converting dp dimensions to pixels for size / touch-target
        checks (e.g. asserting a button is at least 48dp), getting the
        screen's density factor for layout math.
    NOT FOR: the screen's pixel width/height (that is screen size, not
        density), a full UI state snapshot (use nerve_state), a screenshot
        (use nerve_screenshot).
    TIP: density_factor = dpi / 160 (Android mdpi baseline); multiply a dp
        value by it to get pixels. Cheap — one shell read. Returns ~30 tokens.

    Args:
        device: Device serial.

    Returns:
        Dict with platform/density_dpi/density_factor on success, or
        {"error": ...} on failure.
    """
    dev, platform = _core._resolve_device(device)
    if dev is None:
        return {"error": platform}

    ctrl = _core._get_controller(platform)
    try:
        return {"platform": platform, **ctrl.get_density(device)}
    except Exception as exc:
        return {"error": f"Density read failed: {exc}"}
