"""Tests for auto-selecting device from a single caller reservation (linc-nerve-6xem).

Covers:
- Pure helper ``_pick_reserved_device(reservations, caller)`` (one, two, zero leases,
  and ignoring other owners' leases).
- ``device_tool`` wrapper auto-selection for ``nerve_ensure_ready``, ``nerve_launch``,
  and ``nerve_release`` only:
  1. One lease -> picked, ``_core._device_semaphores`` keyed on that serial, result
     carries ``device_auto_selected: True`` and ``device: <serial>``.
  2. Two leases -> error listing both held serials.
  3. Zero leases -> error ``"device is required: you hold no reservation; "
     "pass device or nerve_reserve first"`` (even when a device is online or
     reserved by another owner).
  4. Non-opted tool (``nerve_tap``) with empty ``device=""`` -> unchanged error;
     missing ``device`` -> FastMCP validation error.
  5. Guarded session active on the picked serial still refuses a cross-app ``nerve_launch``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError as FastMCPToolError

from linc_nerve.cli.main import _registry_server
from linc_nerve.core import action_log
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.device_manager import DeviceManager
from linc_nerve.mcp import _core
from linc_nerve.mcp.actionlog_tools import _nerve_actionlog_start_sync
from tests.test_guarded_session import FakeController

TARGET = "com.example.app"


class ReadyFakeController(FakeController):
    """FakeController extended with ensure_ready + get_network_info for state_tools."""

    def ensure_ready(self, serial: str) -> dict[str, Any]:
        self.calls.append(("ensure_ready", (serial,)))
        return {
            "awake": True,
            "unlocked": True,
            "battery_pct": 95,
            "charging": True,
            "ready": True,
            "reason": "ok",
            "actions_taken": [],
            "platform": "android",
        }


def _android_device(serial: str = "PIXEL8PRO") -> NerveDevice:
    return NerveDevice(
        serial=serial,
        platform=Platform.ANDROID,
        device_type=DeviceType.PHONE,
        model="Pixel 8 Pro",
        os_version="15",
    )


def _mcp_call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    srv = _registry_server()
    blocks = anyio.run(srv.call_tool, tool_name, arguments)
    if isinstance(blocks, tuple):
        blocks = blocks[0]
    return json.loads(blocks[0].text)


@pytest.fixture(name="env")
def _env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[DeviceManager, ReadyFakeController]]:
    fake = ReadyFakeController()
    mgr = DeviceManager(db_path=tmp_path / "devices.db")
    mgr._heartbeat_stop.set()
    mgr._heartbeat_thread.join(timeout=2)
    action_log.REGISTRY.reset()
    _core._device_semaphores.clear()
    monkeypatch.setenv("NERVE_ACTIONLOG_ROOTS", str(tmp_path))

    def _fake_resolve(serial: str) -> tuple[NerveDevice | None, str]:
        if serial in {"PIXEL8PRO", "PIXEL9PRO"}:
            return _android_device(serial), "android"
        return None, f"Device '{serial}' not found. Run nerve_discover to list devices."

    with (
        patch.object(_core, "_device_mgr", mgr),
        patch.object(_core, "_resolve_device", side_effect=_fake_resolve),
        patch.object(_core, "_get_controller", return_value=fake),
    ):
        yield mgr, fake

    action_log.REGISTRY.reset()
    _core._device_semaphores.clear()
    mgr.close()


# ---------------------------------------------------------------------------
# (a) Pure helper _pick_reserved_device(reservations, caller)
# ---------------------------------------------------------------------------


def test_pick_reserved_device_one_lease() -> None:
    reservations = [
        {"device_serial": "OTHER-DEV", "owner": "someone-else"},
        {"device_serial": "EXPIRED-DEV", "owner": "session-123", "expires_in_s": 0.0},
        {"device_serial": "DEAD-PID-DEV", "owner": "session-123", "owner_alive": False},
        {"device_serial": "PIXEL8PRO", "owner": "session-123", "expires_in_s": 120.0},
    ]
    assert _core._pick_reserved_device(reservations, "session-123") == "PIXEL8PRO"


def test_pick_reserved_device_accepts_mapping_and_dataclass_entries() -> None:
    from linc_nerve.core.device_manager import DeviceReservation

    lease = DeviceReservation(
        device_serial="PIXEL8PRO",
        session_id="session-123",
        pid=1234,
        reserved_at=100.0,
        last_heartbeat=100.0,
        timeout_seconds=300,
        purpose="bvt",
        owner="session-123",
    )
    assert _core._pick_reserved_device({"PIXEL8PRO": lease}, "session-123") == "PIXEL8PRO"
    assert _core._inject_device_arg(("", "pkg"), {}, "PIXEL8PRO") == (("PIXEL8PRO", "pkg"), {})


def test_pick_reserved_device_two_leases_lists_both() -> None:
    reservations = [
        {"device_serial": "PIXEL8PRO", "owner": "session-123"},
        {"device_serial": "PIXEL9PRO", "owner": "session-123"},
    ]
    res = _core._pick_reserved_device(reservations, "session-123")
    assert isinstance(res, dict)
    assert res.get("validation_error") is True
    assert "PIXEL8PRO" in res["error"]
    assert "PIXEL9PRO" in res["error"]


def test_pick_reserved_device_zero_leases() -> None:
    reservations = [
        {"device_serial": "PIXEL8PRO", "owner": "other-owner"},
    ]
    res = _core._pick_reserved_device(reservations, "session-123")
    assert isinstance(res, dict)
    assert res.get("validation_error") is True
    assert (
        res["error"]
        == "device is required: you hold no reservation; pass device or nerve_reserve first"
    )


# ---------------------------------------------------------------------------
# (b)-(d) device_tool wrapper auto-selection on the three opted-in tools
# ---------------------------------------------------------------------------


def test_one_lease_auto_selects_and_keys_semaphore_on_serial(
    env: tuple[DeviceManager, ReadyFakeController],
) -> None:
    mgr, fake = env
    mgr.reserve("PIXEL8PRO", purpose="bvt")
    _core._device_semaphores.clear()

    ready_res = _mcp_call("nerve_ensure_ready", {})
    assert ready_res["ready"] is True
    assert ready_res["device"] == "PIXEL8PRO"
    assert ready_res["device_auto_selected"] is True
    assert ("ensure_ready", ("PIXEL8PRO",)) in fake.calls
    assert "PIXEL8PRO" in _core._device_semaphores
    assert "" not in _core._device_semaphores

    _core._device_semaphores.clear()
    launch_res = _mcp_call("nerve_launch", {"package": TARGET})
    assert launch_res == {
        "success": True,
        "action": "launch",
        "package": TARGET,
        "device": "PIXEL8PRO",
        "device_auto_selected": True,
    }
    assert ("launch", ("PIXEL8PRO", TARGET)) in fake.calls
    assert "PIXEL8PRO" in _core._device_semaphores
    assert "" not in _core._device_semaphores

    _core._device_semaphores.clear()
    release_res = _mcp_call("nerve_release", {})
    assert release_res == {
        "success": True,
        "device": "PIXEL8PRO",
        "device_auto_selected": True,
    }
    assert "PIXEL8PRO" in _core._device_semaphores
    assert "" not in _core._device_semaphores
    assert mgr.check("PIXEL8PRO") is None


def test_release_uses_owner_arg_when_given(
    env: tuple[DeviceManager, ReadyFakeController],
) -> None:
    mgr, _ = env
    mgr.reserve("PIXEL8PRO", owner="lane-a")
    mgr.reserve("PIXEL9PRO")  # owned by mgr.session_id

    release_res = _mcp_call("nerve_release", {"owner": "lane-a"})
    assert release_res == {
        "success": True,
        "device": "PIXEL8PRO",
        "device_auto_selected": True,
    }
    assert mgr.check("PIXEL8PRO") is None
    assert mgr.check("PIXEL9PRO") is not None


def test_two_leases_returns_error_listing_both(
    env: tuple[DeviceManager, ReadyFakeController],
) -> None:
    mgr, fake = env
    mgr.reserve("PIXEL8PRO", purpose="bvt-1")
    mgr.reserve("PIXEL9PRO", purpose="bvt-2")
    _core._device_semaphores.clear()

    for tool, args in (
        ("nerve_ensure_ready", {}),
        ("nerve_launch", {"package": TARGET}),
        ("nerve_release", {}),
    ):
        res = _mcp_call(tool, args)
        assert "error" in res
        assert "PIXEL8PRO" in res["error"]
        assert "PIXEL9PRO" in res["error"]

    assert fake.calls == []
    assert _core._device_semaphores == {}


def test_zero_leases_returns_required_error_never_picks_online_or_foreign_device(
    env: tuple[DeviceManager, ReadyFakeController],
) -> None:
    mgr, fake = env
    # Another owner holds a lease on PIXEL8PRO, and PIXEL9PRO is online but unreserved.
    mgr.reserve("PIXEL8PRO", owner="foreign-owner")
    _core._device_semaphores.clear()

    for tool, args in (
        ("nerve_ensure_ready", {}),
        ("nerve_launch", {"package": TARGET}),
        ("nerve_release", {}),
    ):
        res = _mcp_call(tool, args)
        assert (
            res["error"]
            == "device is required: you hold no reservation; pass device or nerve_reserve first"
        )

    assert fake.calls == []
    assert _core._device_semaphores == {}


def test_explicit_device_does_not_set_device_auto_selected(
    env: tuple[DeviceManager, ReadyFakeController],
) -> None:
    mgr, _ = env
    mgr.reserve("PIXEL8PRO")

    res = _mcp_call("nerve_launch", {"device": "PIXEL8PRO", "package": TARGET})
    assert res == {"success": True, "action": "launch", "package": TARGET}
    assert "device_auto_selected" not in res


def test_non_opted_tool_empty_device_returns_unchanged_error(
    env: tuple[DeviceManager, ReadyFakeController],
) -> None:
    mgr, fake = env
    mgr.reserve("PIXEL8PRO")

    res = _mcp_call("nerve_tap", {"device": "", "x": 100, "y": 200})
    assert res == {"error": "Device '' not found. Run nerve_discover to list devices."}
    assert fake.calls == []

    with pytest.raises(FastMCPToolError):
        anyio.run(_registry_server().call_tool, "nerve_tap", {"x": 100, "y": 200})


def test_guarded_session_on_picked_serial_still_refuses_cross_app_launch(
    env: tuple[DeviceManager, ReadyFakeController], tmp_path: Path
) -> None:
    mgr, fake = env
    mgr.reserve("PIXEL8PRO")
    log_path = tmp_path / "guarded.jsonl"
    started = _nerve_actionlog_start_sync("PIXEL8PRO", str(log_path), target_package=TARGET)
    assert started["guarded"] is True

    refused = _mcp_call("nerve_launch", {"package": "com.other.app"})
    assert refused["guard"] == "other_package"
    assert refused["detail"] == "com.other.app"
    assert refused["validation_error"] is True
    assert ("launch", ("PIXEL8PRO", "com.other.app")) not in fake.calls

    allowed = _mcp_call("nerve_launch", {"package": TARGET})
    assert allowed["success"] is True
    assert allowed["device"] == "PIXEL8PRO"
    assert allowed["device_auto_selected"] is True
    assert ("launch", ("PIXEL8PRO", TARGET)) in fake.calls
