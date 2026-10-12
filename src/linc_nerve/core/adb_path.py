"""Resolve a usable ``adb`` binary path at import time.

Launchd-managed daemons do not inherit the interactive shell's PATH, so
``shutil.which("adb")`` returns None and ``subprocess.run(["adb", ...])``
raises ``FileNotFoundError``. The scrcpy fast path silently degrades
(see inbox a76efc3e).

Resolution order:

1. ``shutil.which("adb")`` — honors whatever PATH the process actually has.
2. ``$ANDROID_SDK_ROOT/platform-tools/adb`` if env var is set + file exec.
3. ``$ANDROID_HOME/platform-tools/adb`` if env var is set + file exec.
4. ``~/Library/Android/sdk/platform-tools/adb`` — macOS Android Studio default.
5. Literal ``"adb"`` — preserves pre-fix behavior; subprocess will raise
   the original ``FileNotFoundError`` so caller error handling is unchanged.

``ADB_PATH`` is the module-level constant; callers should use it instead
of the literal string ``"adb"``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _is_executable_file(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def resolve_adb_path() -> str:
    """Return the best available ``adb`` binary path. See module docstring."""
    which = shutil.which("adb")
    if which:
        return which

    candidates: list[Path] = []
    for env_var in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        root = os.environ.get(env_var)
        if root:
            candidates.append(Path(root) / "platform-tools" / "adb")

    candidates.append(Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb")

    for candidate in candidates:
        if _is_executable_file(candidate):
            return str(candidate)

    return "adb"


ADB_PATH: str = resolve_adb_path()


# The mini's Homebrew-installed Android Command Line Tools location — the last
# real candidate for the `emulator` binary before giving up (linc-nerve#118).
_HOMEBREW_EMULATOR_PATH = Path("/opt/homebrew/share/android-commandlinetools/emulator/emulator")


def resolve_emulator_path() -> str | None:
    """Return the best available ``emulator`` binary path, or ``None`` if unresolved.

    Mirrors :func:`resolve_adb_path`'s launchd-PATH problem (see module docstring)
    but for the ``emulator`` CLI, with one deliberate difference: an unresolved
    ``emulator`` returns ``None`` instead of falling back to the literal string.
    Unlike ``adb`` (used constantly, on every device-list poll, where a
    FileNotFoundError-on-exec is an acceptable degraded mode), silently returning
    a bare ``"emulator"`` here would mean ``_get_installed_avds`` keeps swallowing
    the same FileNotFoundError it swallows today (reactor 8d140900) — the whole
    point of this resolver is to make "no emulator binary" a distinguishable,
    surfaceable state instead of a silently-empty AVD list.

    Resolution order:

    1. ``shutil.which("emulator")`` — honors whatever PATH the process actually has.
    2. ``$ANDROID_SDK_ROOT/emulator/emulator`` if env var is set + file exec.
    3. ``$ANDROID_HOME/emulator/emulator`` if env var is set + file exec.
    4. ``~/Library/Android/sdk/emulator/emulator`` — macOS Android Studio default.
    5. ``/opt/homebrew/share/android-commandlinetools/emulator/emulator`` — the
       Mac mini bench's Homebrew-installed Android Command Line Tools location.
    6. ``None`` — no usable binary found anywhere.
    """
    which = shutil.which("emulator")
    if which:
        return which

    candidates: list[Path] = []
    for env_var in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        root = os.environ.get(env_var)
        if root:
            candidates.append(Path(root) / "emulator" / "emulator")

    candidates.append(Path.home() / "Library" / "Android" / "sdk" / "emulator" / "emulator")
    candidates.append(_HOMEBREW_EMULATOR_PATH)

    for candidate in candidates:
        if _is_executable_file(candidate):
            return str(candidate)

    return None


def describe_emulator_probe_locations() -> list[str]:
    """Human-readable descriptions of the non-PATH candidates :func:`resolve_emulator_path`
    probes, for surfacing in the ``android_warning`` field when resolution fails
    (``nerve_emulator_list`` — see ``mcp/emulator_tools.py``)."""
    return [
        "$ANDROID_SDK_ROOT/emulator/emulator",
        "$ANDROID_HOME/emulator/emulator",
        "~/Library/Android/sdk/emulator/emulator",
        str(_HOMEBREW_EMULATOR_PATH),
    ]
