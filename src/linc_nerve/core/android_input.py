"""Android input — tap, long-press, swipe, type, key events via ``adb shell input``.

Split from android.py (#53) as a mixin to keep both files under the hot-file LOC
ceiling. ``shlex`` is routed through the parent module (``_android.shlex``) for
uniformity. ``_adb_state`` / ``_settle`` / ``_screen_size`` stay in android.py
because ``ensure_ready`` (which remains in core) is their only caller.
"""

from __future__ import annotations

import re

from . import android as _android

# `input keyevent` accepts KEYCODE_* names, bare names (HOME, back), and
# numeric codes (66) — all match this; shell metacharacters never do.
# Anchored ``\A...\Z`` + ``fullmatch`` (security review 2026-10-02 H1): ``^...$``
# with ``re.match`` accepts a trailing newline.
_KEYCODE_RE = re.compile(r"\A[A-Za-z0-9_]+\Z")


class _InputMixin:
    """Touch + key input methods for AndroidController.

    NOTE: input deliberately does NOT use the scrcpy control socket — it had
    touch coordinate scaling and stream timeout issues. adb shell input is
    the robust path. (The scrcpy input fast path was removed 2026-06-09;
    recover it from git history if it's ever worth retrying.)
    """

    def tap(self, serial: str, x: int, y: int) -> None:
        """Tap at coordinates via adb shell input."""
        self._invalidate_elements_cache(serial)
        self._shell(serial, f"input tap {int(x)} {int(y)}")

    def long_press(self, serial: str, x: int, y: int, duration_ms: int = 500) -> None:
        """Long press at coordinates via adb shell input."""
        self._invalidate_elements_cache(serial)
        self._shell(serial, f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {int(duration_ms)}")

    def swipe(
        self,
        serial: str,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 300,
    ) -> None:
        """Swipe from one point to another via adb shell input."""
        self._invalidate_elements_cache(serial)
        start = f"{int(start_x)} {int(start_y)}"
        end = f"{int(end_x)} {int(end_y)}"
        self._shell(serial, f"input swipe {start} {end} {int(duration_ms)}")

    def type_text(self, serial: str, text: str) -> None:
        """Type text on the focused input field.

        ``adb shell input text`` parses arguments through the device shell:
        spaces split tokens (only the first word would be typed) and
        metacharacters like ``;`` ``&`` ``$`` would be interpreted by ``sh``.
        Replace spaces with ``%s`` (adb's documented space escape) and
        ``shlex.quote`` the rest so the whole payload arrives as a single
        literal argument. See inbox 9432ecdd.
        """
        self._invalidate_elements_cache(serial)
        self._shell(serial, f"input text {_android.shlex.quote(text.replace(' ', '%s'))}")

    def press_key(self, serial: str, key: str) -> None:
        """Press a key event (HOME, BACK, ENTER, etc.).

        ``key`` is interpolated into a device-shell command, so it is
        validated against the keycode grammar (imp ebcb3be3) — a name with
        shell metacharacters is never a legitimate keycode; reject loudly
        rather than quote through a guaranteed-failing keyevent.
        """
        if not _KEYCODE_RE.fullmatch(key):
            raise ValueError(
                f"Invalid key {key!r}: expected a KeyEvent name (HOME, "
                "KEYCODE_BACK) or numeric keycode"
            )
        self._invalidate_elements_cache(serial)
        self._shell(serial, f"input keyevent {_android.shlex.quote(key)}")
