"""Device reservation tools."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from linc_nerve.core.contracts import require
from linc_nerve.core.device_manager import _held_by, _resolve_owner
from linc_nerve.mcp import _core
from linc_nerve.mcp._core import device_tool

if TYPE_CHECKING:
    from linc_nerve.core.device_manager import DeviceManager, DeviceReservation


def _release_error(
    device_mgr: DeviceManager,
    device: str,
    owner: str,
    lease: DeviceReservation | None,
) -> dict[str, Any]:
    """Build the error dict when releasing a device fails."""
    if lease is None:
        return {"error": f"No reservation for '{device}'."}
    resolved_caller = _resolve_owner(owner, device_mgr.session_id)
    since_iso = datetime.fromtimestamp(lease.reserved_at, tz=UTC).isoformat()
    held = _held_by(lease.owner, lease.reserved_at, lease.purpose)
    return {
        "error": (
            f"Reservation for '{device}' is {held}, not by '{resolved_caller}'. "
            f"If that lease is yours, release it with owner='{lease.owner}' "
            f"(CLI: --owner {lease.owner})."
        ),
        "holder": {
            "owner": lease.owner,
            "since": since_iso,
            "purpose": lease.purpose,
            "same_session": lease.session_id == device_mgr.session_id,
        },
    }


@device_tool
def _nerve_reserve_sync(
    device: str,
    purpose: str = "",
    timeout_seconds: int = 300,
    owner: str = "",
    takeover: bool = False,
    owner_pid: int = 0,
) -> dict[str, Any]:
    """Reserve a device exclusively for this session.

    USE FOR: Long-running device work (testing, debugging, deployment) where
    you don't want other sessions interfering.
    NOT FOR: Quick one-off commands (those work without reservation).
    TIP: HTTP callers share ONE daemon session, so pass a stable owner (e.g.
    "nightly-android-watch") or a second lane can silently steal the lease.
    A conflicting owner errors unless takeover=True. Reservations auto-expire
    after timeout_seconds unless renewed: the daemon does NOT keep a
    caller-owned lease alive. Renew by calling reserve again with the same
    owner, or pass owner_pid (e.g. $PPID) so the daemon renews the lease while
    that process is alive and reaps it once that process is gone. A lease
    with --owner-pid outlives daemon restarts until that process exits.

    Args:
        device: Device serial (from nerve_discover).
        purpose: Free text describing what you're using the device for.
        timeout_seconds: Seconds before reservation auto-expires (default 300).
        owner: Stable caller identity; defaults to this session's id.
        takeover: Force-claim a device held by a different owner.
        owner_pid: Caller's own process id (0 = not supplied); the lease is
            reaped when this process exits.
    """
    _core._ensure_initialized()
    device_mgr = require(_core._device_mgr, "device manager")

    result = device_mgr.reserve(
        device,
        purpose=purpose,
        timeout_seconds=timeout_seconds,
        owner=owner,
        takeover=takeover,
        owner_pid=owner_pid,
    )
    if isinstance(result, str):
        return {"error": result}
    if _core._metrics is not None:
        _core._metrics.record_reserve(device)
    return {
        "success": True,
        "device": result.device_serial,
        "session_id": result.session_id,
        "timeout_seconds": result.timeout_seconds,
        "purpose": result.purpose,
        "owner": result.owner,
    }


@device_tool
def _nerve_release_sync(device: str, owner: str = "") -> dict[str, Any]:
    """Release a device reservation.

    USE FOR: Freeing a device when done with it.
    NOT FOR: Devices you haven't reserved.
    TIP: Reservations are also auto-released when the session ends or times
    out. Pass the same owner you reserved with, if you set one.

    Args:
        device: Device serial to release.
        owner: Stable caller identity that holds the reservation; defaults
            to this session's id.
    """
    _core._ensure_initialized()
    device_mgr = require(_core._device_mgr, "device manager")

    released = device_mgr.release(device, owner=owner)
    if released:
        if _core._metrics is not None:
            _core._metrics.record_release(device)
        return {"success": True, "device": device}
    lease = device_mgr.check(device)
    return _release_error(device_mgr, device, owner, lease)
