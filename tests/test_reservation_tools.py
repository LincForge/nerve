"""Tests for the nerve_reserve / nerve_release MCP tools — owner identity.

linc-nerve-enz.3: the :8111 daemon is a single process, so every HTTP caller
shares one ``DeviceManager`` session_id. These tests pin the tool-layer
plumbing for the caller-supplied ``owner`` argument that lets two HTTP lanes
avoid silently stealing the same lease.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.core.ios import IOSController
from linc_nerve.mcp import server as mcp_mod


def _reset_singletons() -> None:
    """Reset module-level singletons between tests (mirrors tests/test_mcp.py)."""
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
        serial="PIXEL8PRO",
        platform=Platform.ANDROID,
        device_type=DeviceType.PHONE,
        model="Pixel 8 Pro",
        os_version="14",
    )


def _wire_mocks() -> None:
    mock_android = MagicMock(spec=AndroidController)
    mock_android.discover.return_value = [_phone()]
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    mcp_mod._android = mock_android
    mcp_mod._ios = mock_ios


class TestNerveReserveOwner:
    def test_conflicting_owner_returns_error_mentioning_blocking_owner(
        self, tmp_path: Any
    ) -> None:
        _reset_singletons()
        _wire_mocks()
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        mcp_mod._device_mgr = mgr
        try:
            first = mcp_mod._nerve_reserve_sync("PIXEL8PRO", owner="lane-a")
            assert first["success"] is True

            second = mcp_mod._nerve_reserve_sync("PIXEL8PRO", owner="lane-b")
            assert "error" in second
            assert "lane-a" in second["error"]
        finally:
            mgr.close()

    def test_successful_reserve_result_contains_owner(self, tmp_path: Any) -> None:
        _reset_singletons()
        _wire_mocks()
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        mcp_mod._device_mgr = mgr
        try:
            result = mcp_mod._nerve_reserve_sync("PIXEL8PRO", owner="lane-a")
            assert result["success"] is True
            assert result["owner"] == "lane-a"
        finally:
            mgr.close()

    def test_default_owner_behaves_like_today(self, tmp_path: Any) -> None:
        """No owner supplied: resolves to this session's own id, unchanged."""
        _reset_singletons()
        _wire_mocks()
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        mcp_mod._device_mgr = mgr
        try:
            result = mcp_mod._nerve_reserve_sync("PIXEL8PRO", purpose="testing")
            assert result["success"] is True
            assert result["session_id"] == mgr.session_id
            assert result["owner"] == mgr.session_id
        finally:
            mgr.close()

    def test_takeover_reowns_a_conflicting_reservation(self, tmp_path: Any) -> None:
        _reset_singletons()
        _wire_mocks()
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        mcp_mod._device_mgr = mgr
        try:
            first = mcp_mod._nerve_reserve_sync("PIXEL8PRO", owner="lane-a")
            assert first["success"] is True

            taken = mcp_mod._nerve_reserve_sync("PIXEL8PRO", owner="lane-b", takeover=True)
            assert taken["success"] is True
            assert taken["owner"] == "lane-b"
        finally:
            mgr.close()


def _mgr(tmp_path: Any, name: str = "test.db") -> DeviceManager:
    mgr = DeviceManager(db_path=tmp_path / name)
    mgr._heartbeat_stop.set()
    mgr._heartbeat_thread.join(timeout=2)
    return mgr


class TestNerveReleaseOwner:
    def test_release_with_no_owner_names_holder_and_preserves_lease(self, tmp_path: Any) -> None:
        _reset_singletons()
        _wire_mocks()
        mgr = _mgr(tmp_path)
        mcp_mod._device_mgr = mgr
        try:
            reserve_res = mcp_mod._nerve_reserve_sync("D1", owner="lane-a")
            assert reserve_res.get("success") is True

            result = mcp_mod._nerve_release_sync("D1")
            assert "error" in result
            assert "lane-a" in result["error"]
            assert "--owner lane-a" in result["error"]
            assert result.get("holder", {}).get("same_session") is True
            assert result["holder"]["owner"] == "lane-a"
            assert mgr.check("D1") is not None
        finally:
            mgr.close()

    def test_release_held_by_another_session_returns_same_session_false(
        self, tmp_path: Any
    ) -> None:
        _reset_singletons()
        _wire_mocks()
        mgr1 = _mgr(tmp_path, "shared.db")
        mgr2 = _mgr(tmp_path, "shared.db")
        mcp_mod._device_mgr = mgr2
        try:
            reserve_res = mgr1.reserve("D1", owner="lane-a")
            assert not isinstance(reserve_res, str)

            result = mcp_mod._nerve_release_sync("D1")
            assert "error" in result
            assert "lane-a" in result["error"]
            assert "--owner lane-a" in result["error"]
            assert result.get("holder", {}).get("same_session") is False
            assert result["holder"]["owner"] == "lane-a"
            assert mgr2.check("D1") is not None
        finally:
            mgr1.close()
            mgr2.close()

    def test_release_unreserved_device_returns_no_reservation(self, tmp_path: Any) -> None:
        _reset_singletons()
        _wire_mocks()
        mgr = _mgr(tmp_path)
        mcp_mod._device_mgr = mgr
        try:
            result = mcp_mod._nerve_release_sync("D1")
            assert result == {"error": "No reservation for 'D1'."}
            assert "holder" not in result
        finally:
            mgr.close()

    def test_release_with_matching_owner_succeeds(self, tmp_path: Any) -> None:
        _reset_singletons()
        _wire_mocks()
        mgr = _mgr(tmp_path)
        mcp_mod._device_mgr = mgr
        try:
            mcp_mod._nerve_reserve_sync("D1", owner="lane-a")
            result = mcp_mod._nerve_release_sync("D1", owner="lane-a")
            assert result["success"] is True
            assert mgr.check("D1") is None
        finally:
            mgr.close()

    def test_release_with_wrong_owner_fails(self, tmp_path: Any) -> None:
        _reset_singletons()
        _wire_mocks()
        mgr = _mgr(tmp_path)
        mcp_mod._device_mgr = mgr
        try:
            mcp_mod._nerve_reserve_sync("PIXEL8PRO", owner="lane-a")
            result = mcp_mod._nerve_release_sync("PIXEL8PRO", owner="lane-b")
            assert "error" in result
            assert "lane-a" in result["error"]
            assert "--owner lane-a" in result["error"]
            assert result.get("holder", {}).get("same_session") is True
            assert mgr.check("PIXEL8PRO") is not None
        finally:
            mgr.close()
