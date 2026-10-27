"""nerve_screenshot on a serial the controller cannot service at all.

A physical Apple Watch has no WDA support: IOSController.screenshot() raises
NotImplementedError instead of silently substituting the paired iPhone's
frame (reactor d94ec895). _nerve_screenshot_sync must surface that as a
distinct "unsupported" shape — not the generic transient-failure shape a
RuntimeError gets — so callers (and the :8111 daemon) know not to retry.
"""

from __future__ import annotations

import glob
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.ios import IOSController
from linc_nerve.mcp import _core
from linc_nerve.mcp import server as mcp_mod

WATCH_SERIAL = "00008310-001A2D3E1401A01E"


def _reset_singletons() -> None:
    if mcp_mod._device_mgr is not None:
        mcp_mod._device_mgr.close()
    mcp_mod._android = None
    mcp_mod._ios = None
    mcp_mod._state_mgr = None
    mcp_mod._metrics = None
    mcp_mod._device_mgr = None
    mcp_mod._emulator = None
    mcp_mod._cached_devices = []
    mcp_mod._cache_timestamp = 0.0
    mcp_mod._cleanup_done = False


def _watch() -> NerveDevice:
    return NerveDevice(
        serial=WATCH_SERIAL,
        platform=Platform.IOS,
        device_type=DeviceType.WATCH,
        model="Apple Watch Series 10",
        os_version="11.0",
    )


def test_screenshot_not_implemented_returns_unsupported_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NotImplementedError -> unsupported dict, no screenshot key, no file written."""
    # Isolated temp root: _output_dir() must never create in (or sweep) the real $TMPDIR.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(_core, "_OUTPUT_DIR", None)
    _reset_singletons()
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = []
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = [_watch()]
    mock_ios.screenshot.side_effect = NotImplementedError(
        f"WDA not supported on watchOS device {WATCH_SERIAL}: screenshot/type/key "
        "unsupported for a physical Apple Watch — use a watchOS simulator or "
        "`xcrun devicectl` app-level checks."
    )
    mcp_mod._android = mock_android
    mcp_mod._ios = mock_ios

    before = set(glob.glob(os.path.join(_core._output_dir(), "*")))
    result = mcp_mod._nerve_screenshot_sync(WATCH_SERIAL)
    after = set(glob.glob(os.path.join(_core._output_dir(), "*")))

    assert result["unsupported"] is True
    assert result["error"].startswith("unsupported:")
    assert "screenshot" not in result
    assert result["device"] == WATCH_SERIAL
    assert after == before, "no file should be written to the output dir"


def test_screenshot_runtime_error_keeps_existing_shape() -> None:
    """A RuntimeError (transient failure) keeps the pre-existing error shape,
    with no `unsupported` key — it is NOT the same as an unsupported serial."""
    _reset_singletons()
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = []
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = [_watch()]
    mock_ios.screenshot.side_effect = RuntimeError("WDA session timed out")
    mcp_mod._android = mock_android
    mcp_mod._ios = mock_ios

    result = mcp_mod._nerve_screenshot_sync(WATCH_SERIAL)

    assert result["error"] == "Screenshot failed: WDA session timed out"
    assert "unsupported" not in result
    assert "screenshot" not in result
