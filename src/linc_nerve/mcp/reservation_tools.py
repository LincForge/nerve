"""Device reservation tools."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from linc_nerve.core.contracts import require
from linc_nerve.core.device_manager import _held_by, _resolve_owner
from linc_nerve.core.procs import pid_alive
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


def _renewal_mode(
    renew_anonymous: bool | DeviceManager,
    shared_owner: bool | DeviceReservation = False,
    owner_pid: int | None = 0,
) -> str:
    """Determine lease renewal authority: 'daemon' if auto-renewed, else 'caller'."""
    if not isinstance(renew_anonymous, bool):
        mgr = renew_anonymous
        res = shared_owner
        r_anon = getattr(mgr, "renew_anonymous", True)
        s_owner = res.owner == mgr.session_id if hasattr(res, "owner") else False
        pid = owner_pid or getattr(res, "owner_pid", None) or 0
        return _renewal_mode(r_anon, s_owner, pid)

    pid = owner_pid or 0
    if (pid > 0 and pid_alive(pid)) or (renew_anonymous and shared_owner):
        return "daemon"
    return "caller"


def _reserve_result(
    device_mgr: DeviceManager,
    result: DeviceReservation,
    owner_pid: int = 0,
) -> dict[str, Any]:
    """Success dict for ``nerve_reserve``: the lease plus whether it is
    anonymous (owned by this process's shared session id) and renewed."""
    shared_owner = result.owner == device_mgr.session_id
    effective_pid = owner_pid or getattr(result, "owner_pid", None) or 0
    renewal = _renewal_mode(
        getattr(device_mgr, "renew_anonymous", True),
        shared_owner,
        effective_pid,
    )
    out: dict[str, Any] = {
        "success": True,
        "device": result.device_serial,
        "session_id": result.session_id,
        "timeout_seconds": result.timeout_seconds,
        "purpose": result.purpose,
        "owner": result.owner,
        "shared_owner": shared_owner,
        "renewed": result.renewed,
        "expires_in_s": result.timeout_seconds,
        "renewal": renewal,
    }
    if shared_owner:
        out["warning"] = (
            "anonymous lease: owned by this nerve process's session id "
            f"{device_mgr.session_id}; every caller of this process without an owner "
            "shares it and can renew or release it. Pass owner (CLI --owner or NERVE_OWNER)."
        )
    return out


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
        timeout_seconds: Seconds before reservation auto-expires (default 300);
            under the daemon, anonymous leases expire after this timeout
            unless re-reserved (or kept alive with owner_pid).
        owner: Stable caller identity (CLI: --owner, else $NERVE_OWNER);
            defaults to pid:<owner_pid> if owner_pid is set, else this
            session's id (shared by every owner-less caller).
        takeover: Force-claim a device held by a different owner; refused
            unless owner or owner_pid names you.
        owner_pid: Caller's own process id (0 = not supplied); the lease is
            reaped when this process exits. Without owner, the lease is
            owned by pid:<owner_pid> (release with that owner).
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
    return _reserve_result(_core._device_mgr, result, owner_pid=owner_pid)


@device_tool
def _nerve_release_sync(
    device: Annotated[str, Field(default="")],
    owner: str = "",
) -> dict[str, Any]:
    """Release a device reservation.

    USE FOR: Freeing a device when done with it.
    NOT FOR: Devices you haven't reserved.
    TIP: Reservations are also auto-released when the session ends or times
    out. Pass the same owner you reserved with, if you set one.

    Args:
        device: Device serial to release (optional when the caller holds a single reservation).
        owner: Stable caller identity that holds the reservation (CLI:
            --owner, else $NERVE_OWNER); defaults to this session's id. A
            lease reserved with only owner_pid is owned by pid:<owner_pid>.
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
