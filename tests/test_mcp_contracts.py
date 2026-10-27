"""MCP schema contract tests for reserve/release/install tools.

Validates parameter enforcement, default values, response shapes, and
error contracts — ensuring the MCP tool API stays stable.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.ios import IOSController
from linc_nerve.mcp import server as mcp_mod


def _reset() -> None:
    """Reset module singletons between tests."""
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


def _phone() -> NerveDevice:
    return NerveDevice(
        serial="CONTRACT-PHONE",
        platform=Platform.ANDROID,
        device_type=DeviceType.PHONE,
        model="Test Phone",
        os_version="14",
    )


def _setup_android() -> MagicMock:
    """Wire up mock controllers with one discoverable Android phone."""
    _reset()
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = [_phone()]
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    mcp_mod._android = mock_android
    mcp_mod._ios = mock_ios
    return mock_android


# ---------------------------------------------------------------------------
# nerve_reserve contracts
# ---------------------------------------------------------------------------


class TestNerveReserveContract:
    def test_required_param_device_missing(self, tmp_path: Any) -> None:
        """Calling reserve without device raises TypeError."""
        _setup_android()
        mgr = DeviceManager(db_path=tmp_path / "r.db")
        mcp_mod._device_mgr = mgr
        try:
            with pytest.raises(TypeError):
                mcp_mod._nerve_reserve_sync()  # type: ignore[call-arg]
        finally:
            mgr.close()

    def test_optional_defaults(self, tmp_path: Any) -> None:
        """purpose defaults to '' and timeout_seconds to 300."""
        _setup_android()
        mgr = DeviceManager(db_path=tmp_path / "r.db")
        mcp_mod._device_mgr = mgr
        try:
            result = mcp_mod._nerve_reserve_sync("CONTRACT-PHONE")
            assert result["success"] is True
            assert result["purpose"] == ""
            assert result["timeout_seconds"] == 300
        finally:
            mgr.close()

    def test_success_response_shape(self, tmp_path: Any) -> None:
        """Success dict has exactly the contracted keys with correct types."""
        _setup_android()
        mgr = DeviceManager(db_path=tmp_path / "r.db")
        mcp_mod._device_mgr = mgr
        try:
            result = mcp_mod._nerve_reserve_sync(
                "CONTRACT-PHONE", purpose="ci", timeout_seconds=60
            )
            assert set(result.keys()) == {
                "success",
                "device",
                "session_id",
                "timeout_seconds",
                "purpose",
                "owner",
                # linc-nerve-jtt8: anonymous-lease flag + renewal report
                "shared_owner",
                "renewed",
                "expires_in_s",
                "warning",
                # linc-nerve-l7pm: lease renewal authority
                "renewal",
            }
            assert result["success"] is True
            assert isinstance(result["device"], str)
            assert isinstance(result["session_id"], str)
            assert isinstance(result["timeout_seconds"], int)
            assert isinstance(result["purpose"], str)
            assert isinstance(result["owner"], str)
            assert result["device"] == "CONTRACT-PHONE"
            assert result["purpose"] == "ci"
            assert result["timeout_seconds"] == 60
            # linc-nerve-enz.3: no owner supplied -> defaults to this session's id
            assert result["owner"] == result["session_id"]
            assert result["shared_owner"] is True
            assert result["renewed"] is False
            assert result["expires_in_s"] == 60
            assert result["renewal"] == "daemon"
        finally:
            mgr.close()

    def test_error_response_shape(self, tmp_path: Any) -> None:
        """Error response has 'error' key with string value."""
        _setup_android()
        db = tmp_path / "r.db"
        # Reserve from a different DeviceManager session to block ours
        blocker = DeviceManager(db_path=db)
        blocker.reserve("CONTRACT-PHONE", purpose="blocker")

        mgr = DeviceManager(db_path=db)
        mcp_mod._device_mgr = mgr
        try:
            result = mcp_mod._nerve_reserve_sync("CONTRACT-PHONE")
            assert "error" in result
            assert isinstance(result["error"], str)
            assert len(result["error"]) > 0
        finally:
            blocker.close()
            mgr.close()


# ---------------------------------------------------------------------------
# nerve_release contracts
# ---------------------------------------------------------------------------


class TestNerveReleaseContract:
    def test_required_param_device_missing(self) -> None:
        """Calling release without device raises TypeError."""
        _setup_android()
        with pytest.raises(TypeError):
            mcp_mod._nerve_release_sync()  # type: ignore[call-arg]

    def test_success_response_shape(self, tmp_path: Any) -> None:
        """Success dict has exactly {success, device} with correct types."""
        _setup_android()
        mgr = DeviceManager(db_path=tmp_path / "r.db")
        mcp_mod._device_mgr = mgr
        try:
            mcp_mod._nerve_reserve_sync("CONTRACT-PHONE")
            result = mcp_mod._nerve_release_sync("CONTRACT-PHONE")
            assert set(result.keys()) == {"success", "device"}
            assert result["success"] is True
            assert isinstance(result["device"], str)
            assert result["device"] == "CONTRACT-PHONE"
        finally:
            mgr.close()

    def test_error_response_shape(self, tmp_path: Any) -> None:
        """Releasing an unreserved device returns error dict."""
        _setup_android()
        mgr = DeviceManager(db_path=tmp_path / "r.db")
        mcp_mod._device_mgr = mgr
        try:
            result = mcp_mod._nerve_release_sync("CONTRACT-PHONE")
            assert "error" in result
            assert isinstance(result["error"], str)
            assert len(result["error"]) > 0
        finally:
            mgr.close()

    def test_error_has_only_error_key(self, tmp_path: Any) -> None:
        """Error response contains only the 'error' key — no extra fields."""
        _setup_android()
        mgr = DeviceManager(db_path=tmp_path / "r.db")
        mcp_mod._device_mgr = mgr
        try:
            result = mcp_mod._nerve_release_sync("CONTRACT-PHONE")
            assert set(result.keys()) == {"error"}
        finally:
            mgr.close()


# ---------------------------------------------------------------------------
# nerve_install contracts
# ---------------------------------------------------------------------------


class TestNerveInstallContract:
    def test_required_param_device_missing(self) -> None:
        """Calling install without device raises TypeError."""
        _setup_android()
        with pytest.raises(TypeError):
            mcp_mod._nerve_install_sync()  # type: ignore[call-arg]

    def test_required_param_path_missing(self) -> None:
        """Calling install without path raises TypeError."""
        _setup_android()
        with pytest.raises(TypeError):
            mcp_mod._nerve_install_sync("CONTRACT-PHONE")  # type: ignore[call-arg]

    def test_success_response_shape(self) -> None:
        """Success dict has exactly {success, action, path} with correct types."""
        mock_android = _setup_android()
        mock_android.install.return_value = None
        result = mcp_mod._nerve_install_sync("CONTRACT-PHONE", "/tmp/app.apk")
        assert set(result.keys()) == {"success", "action", "path"}
        assert result["success"] is True
        assert result["action"] == "install"
        assert isinstance(result["path"], str)
        assert result["path"] == "/tmp/app.apk"

    def test_error_response_shape(self) -> None:
        """Install failure returns error dict with string message."""
        mock_android = _setup_android()
        mock_android.install.side_effect = RuntimeError("APK not found")
        result = mcp_mod._nerve_install_sync("CONTRACT-PHONE", "/bad/path.apk")
        assert "error" in result
        assert isinstance(result["error"], str)
        assert "Install failed" in result["error"]

    def test_unknown_device_returns_error(self) -> None:
        """Installing to unknown device returns error, not exception."""
        _setup_android()
        result = mcp_mod._nerve_install_sync("UNKNOWN-SERIAL", "/tmp/app.apk")
        assert "error" in result
        assert isinstance(result["error"], str)
