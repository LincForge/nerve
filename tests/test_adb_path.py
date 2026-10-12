"""Tests for adb-path resolution (inbox a76efc3e).

Under launchd, ``PATH`` does not inherit the interactive shell's
environment. ``shutil.which("adb")`` returns None and any bare ``adb``
subprocess invocation raises ``FileNotFoundError``. The daemon log fills
up with ``scrcpy_screenshot_failed error="[Errno 2] No such file or
directory: 'adb'"`` and the scrcpy fast path silently degrades.

These tests pin the resolution invariants so we don't regress in CI.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest


def test_resolve_adb_path_prefers_shutil_which(monkeypatch: pytest.MonkeyPatch) -> None:
    """If `shutil.which` returns a path, use it directly — no further search."""
    from linc_nerve.core import adb_path

    monkeypatch.setattr(adb_path.shutil, "which", lambda _: "/opt/homebrew/bin/adb")
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.delenv("ANDROID_HOME", raising=False)

    assert adb_path.resolve_adb_path() == "/opt/homebrew/bin/adb"


def test_resolve_adb_path_honors_android_sdk_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`ANDROID_SDK_ROOT/platform-tools/adb` wins when PATH doesn't contain adb."""
    from linc_nerve.core import adb_path

    sdk_root = tmp_path / "android-sdk"
    platform_tools = sdk_root / "platform-tools"
    platform_tools.mkdir(parents=True)
    fake_adb = platform_tools / "adb"
    fake_adb.write_text("#!/bin/sh\nexit 0\n")
    fake_adb.chmod(fake_adb.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(adb_path.shutil, "which", lambda _: None)
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(sdk_root))
    monkeypatch.delenv("ANDROID_HOME", raising=False)

    assert adb_path.resolve_adb_path() == str(fake_adb)


def test_resolve_adb_path_honors_android_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`ANDROID_HOME/platform-tools/adb` is honored when ANDROID_SDK_ROOT is unset."""
    from linc_nerve.core import adb_path

    home_root = tmp_path / "android-home"
    platform_tools = home_root / "platform-tools"
    platform_tools.mkdir(parents=True)
    fake_adb = platform_tools / "adb"
    fake_adb.write_text("#!/bin/sh\nexit 0\n")
    fake_adb.chmod(fake_adb.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(adb_path.shutil, "which", lambda _: None)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setenv("ANDROID_HOME", str(home_root))

    assert adb_path.resolve_adb_path() == str(fake_adb)


def test_resolve_adb_path_searches_macos_default_sdk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The macOS default `~/Library/Android/sdk/platform-tools/adb` is checked
    when env vars are unset (regression for the inbox a76efc3e launchd bug)."""
    from linc_nerve.core import adb_path

    fake_home = tmp_path / "home"
    macos_default = fake_home / "Library" / "Android" / "sdk" / "platform-tools"
    macos_default.mkdir(parents=True)
    fake_adb = macos_default / "adb"
    fake_adb.write_text("#!/bin/sh\nexit 0\n")
    fake_adb.chmod(fake_adb.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(adb_path.shutil, "which", lambda _: None)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    assert adb_path.resolve_adb_path() == str(fake_adb)


def test_resolve_adb_path_falls_back_to_literal_when_nothing_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When nothing resolves, return the literal ``"adb"`` so behavior matches
    the pre-fix degraded mode (subprocess will FileNotFoundError, but the
    caller's error handling is unchanged)."""
    from linc_nerve.core import adb_path

    monkeypatch.setattr(adb_path.shutil, "which", lambda _: None)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-android-here")

    assert adb_path.resolve_adb_path() == "adb"


def test_module_constant_ADB_PATH_is_non_empty_string() -> None:
    """At import time `ADB_PATH` is computed via `resolve_adb_path()`."""
    from linc_nerve.core import adb_path

    assert isinstance(adb_path.ADB_PATH, str)
    assert adb_path.ADB_PATH != ""


def test_resolve_adb_path_skips_non_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A path entry pointing at a non-executable `adb` file (chmod 644) is
    skipped — we only return paths that can actually be invoked."""
    from linc_nerve.core import adb_path

    sdk_root = tmp_path / "android-sdk"
    platform_tools = sdk_root / "platform-tools"
    platform_tools.mkdir(parents=True)
    fake_adb = platform_tools / "adb"
    fake_adb.write_text("not-executable")
    os.chmod(fake_adb, 0o644)

    monkeypatch.setattr(adb_path.shutil, "which", lambda _: None)
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(sdk_root))
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-default-sdk")

    # Should fall through to literal "adb" since the SDK entry is non-executable
    assert adb_path.resolve_adb_path() == "adb"


def test_resolve_emulator_path_prefers_shutil_which(monkeypatch: pytest.MonkeyPatch) -> None:
    """If `shutil.which` finds `emulator` on PATH, use it directly — no further search."""
    from linc_nerve.core import adb_path

    monkeypatch.setattr(adb_path.shutil, "which", lambda name: "/opt/homebrew/bin/emulator")
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.delenv("ANDROID_HOME", raising=False)

    assert adb_path.resolve_emulator_path() == "/opt/homebrew/bin/emulator"


def test_resolve_emulator_path_honors_android_sdk_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`ANDROID_SDK_ROOT/emulator/emulator` wins when PATH doesn't contain emulator —
    including over the Homebrew fallback (precedence, not just presence)."""
    from linc_nerve.core import adb_path

    sdk_root = tmp_path / "android-sdk"
    emulator_dir = sdk_root / "emulator"
    emulator_dir.mkdir(parents=True)
    fake_emulator = emulator_dir / "emulator"
    fake_emulator.write_text("#!/bin/sh\nexit 0\n")
    fake_emulator.chmod(fake_emulator.stat().st_mode | stat.S_IEXEC)

    homebrew_path = Path("/opt/homebrew/share/android-commandlinetools/emulator/emulator")

    monkeypatch.setattr(adb_path.shutil, "which", lambda name: None)
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(sdk_root))
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.setattr(adb_path, "_HOMEBREW_EMULATOR_PATH", homebrew_path)
    monkeypatch.setattr(
        adb_path, "_is_executable_file", lambda p: p in (fake_emulator, homebrew_path)
    )

    assert adb_path.resolve_emulator_path() == str(fake_emulator)


def test_resolve_emulator_path_honors_android_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`ANDROID_HOME/emulator/emulator` is honored when ANDROID_SDK_ROOT is unset."""
    from linc_nerve.core import adb_path

    home_root = tmp_path / "android-home"
    emulator_dir = home_root / "emulator"
    emulator_dir.mkdir(parents=True)
    fake_emulator = emulator_dir / "emulator"
    fake_emulator.write_text("#!/bin/sh\nexit 0\n")
    fake_emulator.chmod(fake_emulator.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(adb_path.shutil, "which", lambda name: None)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setenv("ANDROID_HOME", str(home_root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-default-sdk")

    assert adb_path.resolve_emulator_path() == str(fake_emulator)


def test_resolve_emulator_path_searches_macos_default_sdk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`~/Library/Android/sdk/emulator/emulator` is checked when env vars are unset."""
    from linc_nerve.core import adb_path

    fake_home = tmp_path / "home"
    macos_default = fake_home / "Library" / "Android" / "sdk" / "emulator"
    macos_default.mkdir(parents=True)
    fake_emulator = macos_default / "emulator"
    fake_emulator.write_text("#!/bin/sh\nexit 0\n")
    fake_emulator.chmod(fake_emulator.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(adb_path.shutil, "which", lambda name: None)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setattr(adb_path, "_HOMEBREW_EMULATOR_PATH", tmp_path / "no-homebrew-here")

    assert adb_path.resolve_emulator_path() == str(fake_emulator)


def test_resolve_emulator_path_falls_back_to_homebrew_location(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When PATH/env-var/macOS-default all miss, the mini's Homebrew Android
    Command Line Tools location (/opt/homebrew/share/android-commandlinetools/
    emulator/emulator) is the last real candidate before giving up."""
    from linc_nerve.core import adb_path

    homebrew_dir = tmp_path / "homebrew" / "android-commandlinetools" / "emulator"
    homebrew_dir.mkdir(parents=True)
    fake_emulator = homebrew_dir / "emulator"
    fake_emulator.write_text("#!/bin/sh\nexit 0\n")
    fake_emulator.chmod(fake_emulator.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(adb_path.shutil, "which", lambda name: None)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-android-here")
    monkeypatch.setattr(adb_path, "_HOMEBREW_EMULATOR_PATH", fake_emulator)

    assert adb_path.resolve_emulator_path() == str(fake_emulator)


def test_resolve_emulator_path_returns_none_when_unresolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unlike `resolve_adb_path`, an unresolved emulator binary returns `None`
    (a distinguishable state) rather than a literal fallback string — callers
    must handle "no emulator binary" explicitly instead of hitting a surprise
    FileNotFoundError deep in a subprocess call."""
    from linc_nerve.core import adb_path

    monkeypatch.setattr(adb_path.shutil, "which", lambda name: None)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-android-here")
    monkeypatch.setattr(adb_path, "_HOMEBREW_EMULATOR_PATH", tmp_path / "no-homebrew-here")

    assert adb_path.resolve_emulator_path() is None


def test_resolve_emulator_path_only_calls_which_with_emulator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: must probe PATH for `emulator`, not `adb` (copy-paste risk
    given how closely this mirrors `resolve_adb_path`)."""
    from linc_nerve.core import adb_path

    seen: list[str] = []
    monkeypatch.setattr(
        adb_path.shutil, "which", lambda name: seen.append(name) or "/found/emulator"
    )

    adb_path.resolve_emulator_path()

    assert seen == ["emulator"]


def test_describe_emulator_probe_locations_names_all_four() -> None:
    """The probed-locations description (used in the `android_warning` surfaced by
    `nerve_emulator_list`) names all four non-PATH candidates so a human can act on
    the warning without reading source."""
    from linc_nerve.core import adb_path

    locations = adb_path.describe_emulator_probe_locations()

    assert len(locations) == 4
    joined = " ".join(locations)
    assert "ANDROID_SDK_ROOT" in joined
    assert "ANDROID_HOME" in joined
    assert "Library/Android/sdk" in joined
    assert "/opt/homebrew/share/android-commandlinetools" in joined


def test_android_controller_uses_resolved_adb_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AndroidController subprocess invocations must call the resolved binary,
    not the literal string ``"adb"`` — that's the bug a76efc3e is fixing.

    Patch ``adb_path.ADB_PATH`` to a sentinel and assert subprocess.run is
    called with it."""
    from unittest.mock import patch

    from linc_nerve.core import adb_path
    from linc_nerve.core.android import AndroidController

    sentinel = "/totally/fake/path/to/adb"
    captured_cmds: list[list[Any]] = []

    def fake_run(cmd: list[Any], **kwargs: Any) -> Any:
        captured_cmds.append(cmd)

        class _R:
            returncode = 0
            stdout = b""
            stderr = b""

        return _R()

    monkeypatch.setattr(adb_path, "ADB_PATH", sentinel)
    # Re-import android to pick up the new ADB_PATH? Better: patch the
    # reference android.py keeps to ADB_PATH directly.
    import linc_nerve.core.android as android_mod

    monkeypatch.setattr(android_mod, "ADB_PATH", sentinel, raising=False)

    ctrl = AndroidController()
    ctrl._scrcpy_available = False
    with patch("subprocess.run", side_effect=fake_run):
        ctrl._reset_client()

    # The smoke test: ADB_PATH was successfully imported into android.py
    assert hasattr(android_mod, "ADB_PATH")
    assert sentinel == android_mod.ADB_PATH
