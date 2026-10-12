"""Device reservation manager — cross-process device coordination via SQLite.

Multiple linc-nerve MCP server instances (one per Claude Code session) coordinate
device access through a shared SQLite WAL database. Each process claims devices
via reservations, heartbeats to prove liveness, and a reaper cleans up after
dead processes.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

from linc_nerve.core.procs import pid_alive

logger = structlog.get_logger().bind(domain="device_control")

_HEARTBEAT_INTERVAL = 30  # seconds


def _default_db_path() -> Path:
    """Resolve default devices.db path lazily; honors LINC_HOME for test isolation."""
    home = os.getenv("LINC_HOME")
    if home:
        return Path(home) / ".local" / "share" / "linc-nerve" / "devices.db"
    return Path.home() / ".local" / "share" / "linc-nerve" / "devices.db"


def _effective_owner(raw_owner: str, session_id: str) -> str:
    """Resolve a stored ``owner`` column to the identity that actually holds
    a row.

    A row with ``owner == ''`` predates the owner column (migrated via
    ``ALTER TABLE ... ADD COLUMN owner TEXT NOT NULL DEFAULT ''``) or was
    reserved by a caller that never passed one — both read as owned by the
    session that created it, i.e. today's session_id-only semantics.
    """
    return raw_owner or session_id


def _held_by(owner: str, reserved_at: float, purpose: str) -> str:
    """Human string naming who holds a lease, since when, and why."""
    since = datetime.fromtimestamp(reserved_at, tz=UTC).isoformat()
    return f"held by {owner} since {since} (purpose: {purpose or 'unspecified'})"


def _resolve_owner(owner: str, session_id: str) -> str:
    """Caller-supplied owner, defaulting to the session id — today's
    semantics for a caller that doesn't pass one."""
    return owner or session_id


def _owner_mismatch(requested: str, lease_owner: str, session_id: str) -> bool:
    """True when a fenced caller (non-empty ``requested``) is not the lease's
    effective owner. An empty ``requested`` is unfenced — never a mismatch."""
    if not requested:
        return False
    return _resolve_owner(requested, session_id) != lease_owner


def _guard_refusal(serial: str, reservation: DeviceReservation) -> str:
    """The one refusal ``guard()`` returns — names the holder via ``_held_by``."""
    held = _held_by(reservation.owner, reservation.reserved_at, reservation.purpose)
    return f"Device '{serial}' is {held}. Use nerve_pool to see all reservations."


@dataclass
class DeviceReservation:
    """A device reservation held by a nerve process instance."""

    device_serial: str
    session_id: str
    pid: int
    reserved_at: float
    last_heartbeat: float
    timeout_seconds: int
    purpose: str
    owner: str


class DeviceManager:
    """Manages device reservations across nerve process instances.

    Uses a shared SQLite WAL database for cross-process coordination.
    Each DeviceManager instance represents one nerve session and can
    reserve, release, and guard devices.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path if db_path is not None else _default_db_path()
        self._session_id = uuid.uuid4().hex[:12]
        self._pid = os.getpid()
        self._closed = False
        self._heartbeat_stop = threading.Event()

        # Create parent dirs and database
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            timeout=5.0,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reservations (
                device_serial TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                pid INTEGER NOT NULL,
                reserved_at REAL NOT NULL,
                last_heartbeat REAL NOT NULL,
                timeout_seconds INTEGER NOT NULL,
                purpose TEXT NOT NULL DEFAULT '',
                owner TEXT NOT NULL DEFAULT '',
                owner_pid INTEGER
            )
            """
        )
        self._conn.commit()
        self._ensure_owner_column()
        self._ensure_owner_pid_column()
        self._lock = threading.Lock()

        # Adopt leases whose storing daemon died but whose owner_pid lives,
        # then clean up stale reservations from dead processes.
        self._adopt_orphaned_leases()
        self._reap_stale()

        # Start heartbeat daemon
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="nerve-heartbeat"
        )
        self._heartbeat_thread.start()

        # Register cleanup on exit
        atexit.register(self._atexit_cleanup)

        logger.debug(
            "device_manager_init",
            session_id=self._session_id,
            pid=self._pid,
            db_path=str(self._db_path),
        )

    @property
    def session_id(self) -> str:
        """This manager's unique session identifier."""
        return self._session_id

    def _ensure_owner_column(self) -> None:
        """Migrate a pre-existing db (created before linc-nerve-enz.3) that
        lacks the ``owner`` column. New tables already get it from
        ``CREATE TABLE`` above, so this is a no-op there."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(reservations)")}
        if "owner" not in cols:
            self._conn.execute(
                "ALTER TABLE reservations ADD COLUMN owner TEXT NOT NULL DEFAULT ''"
            )
            self._conn.commit()

    def _ensure_owner_pid_column(self) -> None:
        """Add the nullable ``owner_pid`` column to a pre-existing db."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(reservations)")}
        if "owner_pid" not in cols:
            self._conn.execute("ALTER TABLE reservations ADD COLUMN owner_pid INTEGER")
            self._conn.commit()

    def reserve(
        self,
        serial: str,
        purpose: str = "",
        timeout_seconds: int = 300,
        *,
        owner: str = "",
        takeover: bool = False,
        owner_pid: int = 0,
    ) -> DeviceReservation | str:
        """Reserve a device exclusively for this session.

        ``owner`` is a caller-supplied identity distinct from ``session_id``:
        the :8111 daemon is one process, so every HTTP caller shares a single
        ``DeviceManager`` (and therefore one session_id) — without a
        caller-supplied owner, a second lane's reserve() on a live row would
        silently succeed and steal the lease. ``owner`` defaults to this
        session's id, preserving pre-owner behavior for callers that don't
        pass one. A live row held by a *different* owner blocks unless
        ``takeover=True``, which force-claims it.

        ``owner_pid`` (0 = not supplied) names the caller's own process. The
        daemon reaps the lease on its tick once that process is gone, and
        keeps renewing it while it is alive. Without it, a caller-owned lease
        is not renewed by the daemon: re-reserve with the same owner to renew,
        or it expires after ``timeout_seconds``.

        Returns a DeviceReservation on success, or an error string if the
        device is already reserved by a different owner.
        """
        now = time.time()
        stored_owner_pid = owner_pid or None
        resolved_owner = _resolve_owner(owner, self._session_id)
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO reservations
                        (device_serial, session_id, pid, reserved_at, last_heartbeat,
                         timeout_seconds, purpose, owner, owner_pid)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        serial,
                        self._session_id,
                        self._pid,
                        now,
                        now,
                        timeout_seconds,
                        purpose,
                        resolved_owner,
                        stored_owner_pid,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                error = self._claim_existing_reservation(
                    serial,
                    resolved_owner,
                    purpose,
                    timeout_seconds,
                    now,
                    takeover,
                    stored_owner_pid,
                )
                if error is not None:
                    return error

        logger.info(
            "device_reserved",
            serial=serial,
            session_id=self._session_id,
            purpose=purpose,
            owner=resolved_owner,
        )
        return DeviceReservation(
            device_serial=serial,
            session_id=self._session_id,
            pid=self._pid,
            reserved_at=now,
            last_heartbeat=now,
            timeout_seconds=timeout_seconds,
            purpose=purpose,
            owner=resolved_owner,
        )

    def _claim_existing_reservation(
        self,
        serial: str,
        resolved_owner: str,
        purpose: str,
        timeout_seconds: int,
        now: float,
        takeover: bool,
        owner_pid: int | None = None,
    ) -> str | None:
        """Handle the INSERT IntegrityError path of ``reserve`` under
        ``self._lock``: update the row in place for an idempotent same-owner
        reserve or an explicit takeover, or return a refusal string.
        """
        row = self._conn.execute(
            "SELECT session_id, purpose, owner, reserved_at "
            "FROM reservations WHERE device_serial = ?",
            (serial,),
        ).fetchone()
        if row is None:
            return f"Device '{serial}' reservation failed unexpectedly."

        existing_session_id, existing_purpose, existing_raw_owner, existing_reserved_at = row
        existing_owner = _effective_owner(existing_raw_owner, existing_session_id)
        is_takeover = existing_owner != resolved_owner

        if is_takeover and not takeover:
            held = _held_by(existing_owner, existing_reserved_at, existing_purpose)
            return (
                f"Device '{serial}' is {held}. Wait for release or timeout, or pass takeover=True."
            )

        # Idempotent same-owner update and explicit takeover both re-point the
        # row at this session/pid — a same-owner reserve from a *different*
        # process must still take over heartbeat duty, or the reaper could
        # kill the row out from under the new holder. A takeover also resets
        # reserved_at (it's a fresh claim); an idempotent same-owner update
        # leaves it alone, matching pre-owner behavior.
        if is_takeover:
            self._conn.execute(
                """
                UPDATE reservations
                SET session_id = ?, pid = ?, reserved_at = ?, purpose = ?,
                    last_heartbeat = ?, timeout_seconds = ?, owner = ?,
                    owner_pid = ?
                WHERE device_serial = ?
                """,
                (
                    self._session_id,
                    self._pid,
                    now,
                    purpose,
                    now,
                    timeout_seconds,
                    resolved_owner,
                    owner_pid,
                    serial,
                ),
            )
            logger.info(
                "device_reservation_taken_over",
                serial=serial,
                old_owner=existing_owner,
                new_owner=resolved_owner,
            )
        else:
            self._conn.execute(
                """
                UPDATE reservations
                SET session_id = ?, pid = ?, purpose = ?, last_heartbeat = ?,
                    timeout_seconds = ?, owner = ?, owner_pid = ?
                WHERE device_serial = ?
                """,
                (
                    self._session_id,
                    self._pid,
                    purpose,
                    now,
                    timeout_seconds,
                    resolved_owner,
                    owner_pid,
                    serial,
                ),
            )
        self._conn.commit()
        return None

    def release(self, serial: str, *, owner: str = "") -> bool:
        """Release a reservation held by ``owner`` (default: this session's id).

        Returns True if a reservation was released, False otherwise —
        including when the reservation is held by a different owner.
        """
        resolved_owner = _resolve_owner(owner, self._session_id)
        with self._lock:
            # A stored owner of '' predates the owner column / was never set —
            # today's semantics read that as owned by its session_id (see
            # _effective_owner). Match either form directly in SQL so this
            # method keeps a single decision point, same as before owner
            # existed.
            cursor = self._conn.execute(
                """
                DELETE FROM reservations
                WHERE device_serial = ?
                  AND (owner = ? OR (owner = '' AND session_id = ?))
                """,
                (serial, resolved_owner, resolved_owner),
            )
            self._conn.commit()
            released = cursor.rowcount > 0

        if released:
            logger.info(
                "device_released", serial=serial, session_id=self._session_id, owner=resolved_owner
            )
        return released

    def release_all(self) -> int:
        """Release all reservations held by this session, except leases whose
        ``owner_pid`` is still alive — those outlive this manager (``close()``
        and the atexit hook) and are adopted by the next daemon.

        Returns the number of reservations released.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT device_serial, owner_pid FROM reservations WHERE session_id = ?",
                (self._session_id,),
            ).fetchall()
            released = _releasable_on_close(rows)
            self._conn.executemany("DELETE FROM reservations WHERE device_serial = ?", released)
            self._conn.commit()

        count = len(released)
        self._log_released_all(count, len(rows) - count)
        return count

    def _log_released_all(self, count: int, kept: int) -> None:
        """Log a ``release_all`` that touched any of this session's rows."""
        if count or kept:
            logger.info(
                "devices_released_all",
                session_id=self._session_id,
                count=count,
                kept_live_owner=kept,
            )

    def check(self, serial: str) -> DeviceReservation | None:
        """Check if a device has an active reservation.

        Returns the DeviceReservation if reserved, None otherwise.
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT device_serial, session_id, pid, reserved_at,
                       last_heartbeat, timeout_seconds, purpose, owner
                FROM reservations WHERE device_serial = ?
                """,
                (serial,),
            ).fetchone()

        if row is None:
            return None
        return DeviceReservation(
            device_serial=row[0],
            session_id=row[1],
            pid=row[2],
            reserved_at=row[3],
            last_heartbeat=row[4],
            timeout_seconds=row[5],
            purpose=row[6],
            owner=_effective_owner(row[7], row[1]),
        )

    def pool(self) -> list[dict]:
        """Return all active reservations with status info."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT device_serial, session_id, pid, reserved_at,
                       last_heartbeat, timeout_seconds, purpose, owner, owner_pid
                FROM reservations ORDER BY reserved_at
                """
            ).fetchall()

        now = time.time()
        return [self._pool_row(row, now) for row in rows]

    def _pool_row(self, row: tuple, now: float) -> dict:
        """Build one ``pool`` dict from a reservations row."""
        since_heartbeat = now - row[4]
        owner = _effective_owner(row[7], row[1])
        owner_pid = row[8]
        return {
            "device_serial": row[0],
            "session_id": row[1],
            "pid": row[2],
            "reserved_at": row[3],
            "age_seconds": round(now - row[3], 1),
            "last_heartbeat": row[4],
            "seconds_since_heartbeat": round(since_heartbeat, 1),
            "timeout_seconds": row[5],
            "purpose": row[6],
            "owner": owner,
            "own": row[1] == self._session_id,
            "owner_pid": owner_pid,
            "owner_alive": None if owner_pid is None else _pid_alive(owner_pid),
            "expires_in_s": round(row[5] - since_heartbeat, 1),
            "held_by": _held_by(owner, row[3], row[6]),
        }

    def is_available(self, serial: str) -> bool:
        """Check if a device is available (unreserved or reserved by this session)."""
        reservation = self.check(serial)
        if reservation is None:
            return True
        return reservation.session_id == self._session_id

    def guard(self, serial: str, *, owner: str = "") -> str | None:
        """Fast-path check for device access.

        Returns None if the device can be used (unreserved, or reserved by
        this session and ``owner`` is empty or matches the lease's owner).
        Returns the ``_held_by`` refusal string if reserved by another
        session, or by this session for a different ``owner`` (audit F5 —
        after #178 adoption every HTTP lease is same-session from the
        daemon's view). Auto-reaps stale reservations BEFORE the owner fence.
        """
        reservation = self.check(serial)
        if reservation is None:
            return None
        reason = self._guard_reap_reason(reservation)
        if reason is not None:
            logger.info(
                "guard_reaping_dead_reservation",
                serial=serial,
                session_id=reservation.session_id,
                pid=reservation.pid,
                reason=reason,
            )
            with self._lock:
                self._conn.execute(
                    "DELETE FROM reservations WHERE device_serial = ?",
                    (serial,),
                )
                self._conn.commit()
            return None
        if reservation.session_id == self._session_id:
            return self._owner_fence(serial, reservation, owner)
        return _guard_refusal(serial, reservation)

    def _owner_fence(self, serial: str, reservation: DeviceReservation, owner: str) -> str | None:
        """Owner fence for a lease this session holds: refuse a mismatched
        owner; let an unfenced (empty-owner) caller through but log it when
        the lease names a distinct owner, so remaining unfenced callers show
        up in the daemon log."""
        if _owner_mismatch(owner, reservation.owner, self._session_id):
            return _guard_refusal(serial, reservation)
        if not owner and reservation.owner != self._session_id:
            logger.info("guard_unfenced_caller", serial=serial, held_by=reservation.owner)
        return None

    def _guard_should_reap(self, reservation: DeviceReservation) -> bool:
        """A lease guard() must reap — exactly when the heartbeat tick's
        ``_reap_stale`` would, so the two can never disagree."""
        return self._guard_reap_reason(reservation) is not None

    def _guard_reap_reason(self, reservation: DeviceReservation) -> str | None:
        """``_lease_stale_reason`` over the stored row for ``reservation``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT pid, owner_pid, last_heartbeat, timeout_seconds "
                "FROM reservations WHERE device_serial = ?",
                (reservation.device_serial,),
            ).fetchone()
        if row is None:
            return None
        pid, owner_pid, last_hb, timeout = row
        return _lease_stale_reason(pid, owner_pid, time.time() - last_hb, timeout)

    def _adopt_orphaned_leases(self) -> None:
        """Re-point leases whose storing daemon is dead but whose owner_pid is
        alive at this manager, so a daemon restart does not drop a lease its
        caller still holds. Rows with a dead owner_pid are left for
        ``_reap_stale`` (``owner_gone``)."""
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT device_serial, session_id, pid, owner, owner_pid "
                "FROM reservations WHERE owner_pid IS NOT NULL"
            ).fetchall()
            adopted = [row for row in rows if _adoptable(row[2], row[4])]
            for serial, session_id, _pid, owner, owner_pid in adopted:
                self._conn.execute(
                    "UPDATE reservations SET session_id = ?, pid = ?, last_heartbeat = ? "
                    "WHERE device_serial = ?",
                    (self._session_id, self._pid, now, serial),
                )
                logger.info(
                    "lease_adopted",
                    serial=serial,
                    owner=_effective_owner(owner, session_id),
                    owner_pid=owner_pid,
                    session_id=self._session_id,
                )
            self._conn.commit()

    def _heartbeat_loop(self) -> None:
        """Background thread: update last_heartbeat, then reap stale reservations.

        Both steps run every tick (issue #94 gap 3 — a prior version only
        reaped once at ``__init__``, so a long-lived session's stale-reservation
        self-heal silently stopped working after the first reap). The two
        steps are wrapped independently: an exception updating this session's
        own heartbeat means the DB is likely closed during shutdown, so the
        loop exits; but a raising ``_reap_stale`` is best-effort cleanup on
        *other* sessions' reservations and must never bring this session's
        heartbeat thread down — that would silently disable the self-heal for
        the rest of the process's life.
        """
        while not self._heartbeat_stop.wait(timeout=_HEARTBEAT_INTERVAL):
            try:
                with self._lock:
                    self._renew_live_leases(time.time())
            except Exception:
                # DB might be closed during shutdown
                break

            try:
                self._reap_stale()
            except Exception:
                logger.warning(
                    "reap_stale_failed",
                    session_id=self._session_id,
                    exc_info=True,
                )

    def _renew_live_leases(self, now: float) -> None:
        """Refresh last_heartbeat for leases whose holder is provably alive:
        rows this manager holds for itself, and rows whose owner_pid is set and
        alive. Caller-owned leases without an owner_pid are NOT renewed here;
        the caller renews by re-reserving. Caller holds ``self._lock``."""
        self._conn.execute(
            "UPDATE reservations SET last_heartbeat = ? "
            "WHERE session_id = ? AND (owner = '' OR owner = session_id)",
            (now, self._session_id),
        )
        pid_rows = self._conn.execute(
            "SELECT device_serial, owner_pid FROM reservations WHERE owner_pid IS NOT NULL"
        ).fetchall()
        for serial, owner_pid in pid_rows:
            if _pid_alive(owner_pid):
                self._conn.execute(
                    "UPDATE reservations SET last_heartbeat = ? WHERE device_serial = ?",
                    (now, serial),
                )
        self._conn.commit()

    def _reap_stale(self) -> None:
        """Delete reservations from dead processes or timed-out sessions."""
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT device_serial, session_id, pid, last_heartbeat, timeout_seconds,
                       owner_pid
                FROM reservations
                """
            ).fetchall()

        stale_serials = []
        for serial, session_id, pid, last_hb, timeout, owner_pid in rows:
            elapsed = now - last_hb
            reason = _lease_stale_reason(pid, owner_pid, elapsed, timeout)
            if reason is not None:
                stale_serials.append(serial)
                logger.info(
                    "reaping_stale_reservation",
                    serial=serial,
                    session_id=session_id,
                    pid=pid,
                    owner_pid=owner_pid,
                    elapsed=round(elapsed, 1),
                    reason=reason,
                )

        if stale_serials:
            with self._lock:
                for serial in stale_serials:
                    self._conn.execute(
                        "DELETE FROM reservations WHERE device_serial = ?",
                        (serial,),
                    )
                self._conn.commit()

    def _atexit_cleanup(self) -> None:
        """Atexit handler — release all and close quietly."""
        if not self._closed:
            with contextlib.suppress(Exception):
                self.release_all()

    def close(self) -> None:
        """Stop heartbeat, release this session's reservations (live-owner_pid
        leases are kept for the next daemon), close database."""
        if self._closed:
            return
        self._closed = True
        self._heartbeat_stop.set()
        with contextlib.suppress(Exception):
            self.release_all()
        with contextlib.suppress(Exception):
            self._conn.close()
        logger.debug("device_manager_closed", session_id=self._session_id)


def _lease_stale_reason(
    pid: int, owner_pid: int | None, elapsed: float, timeout: float
) -> str | None:
    """Why a lease should be reaped, or None if it is still live.

    A set ``owner_pid`` decides liveness on pid grounds before the storing
    ``pid``: the lease belongs to the caller's process, not to the daemon
    that happened to write the row.
    """
    pid_reason = _pid_stale_reason(pid, owner_pid)
    if pid_reason is not None:
        return pid_reason
    if elapsed > timeout:
        return "heartbeat_timeout"
    return None


def _pid_stale_reason(pid: int, owner_pid: int | None) -> str | None:
    """Pid-grounds staleness: owner_pid when set, else the storing pid."""
    if owner_pid is not None:
        return None if _pid_alive(owner_pid) else "owner_gone"
    return None if _pid_alive(pid) else "dead_pid"


def _owner_pid_alive(owner_pid: int | None) -> bool:
    """True if the lease names an owner_pid and that process is alive."""
    return owner_pid is not None and _pid_alive(owner_pid)


def _releasable_on_close(rows: list[tuple[str, int | None]]) -> list[tuple[str]]:
    """``(device_serial,)`` params for the ``(serial, owner_pid)`` rows
    ``release_all`` deletes: those without a live owner_pid."""
    return [(serial,) for serial, owner_pid in rows if not _owner_pid_alive(owner_pid)]


def _adoptable(pid: int, owner_pid: int | None) -> bool:
    """A lease a new manager should adopt: its storing daemon is dead but its
    owner process is alive."""
    return _owner_pid_alive(owner_pid) and not _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    """Check if a process is alive via kill(pid, 0)."""
    return pid_alive(pid)
