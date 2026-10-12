"""Tests for DeviceManager — cross-process device reservation."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from linc_nerve.core.device_manager import (
    DeviceManager,
    DeviceReservation,
    _default_db_path,
    _owner_mismatch,
)


class TestDefaultDbPath:
    """Both branches of _default_db_path. The conftest now sets LINC_HOME
    process-wide (leak isolation), so the unset branch is only reachable by
    explicitly deleting it here."""

    _SUFFIX = Path(".local") / "share" / "linc-nerve" / "devices.db"

    def test_honors_linc_home(self, monkeypatch) -> None:
        monkeypatch.setenv("LINC_HOME", "/tmp/nerve-home")
        assert _default_db_path() == Path("/tmp/nerve-home") / self._SUFFIX

    def test_falls_back_to_home_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("LINC_HOME", raising=False)
        assert _default_db_path() == Path.home() / self._SUFFIX


class TestReserveAndRelease:
    def test_reserve_and_release(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            result = mgr.reserve("DEVICE_A", purpose="testing")
            assert isinstance(result, DeviceReservation)
            assert result.device_serial == "DEVICE_A"
            assert result.session_id == mgr.session_id
            assert result.pid == os.getpid()
            assert result.purpose == "testing"

            check = mgr.check("DEVICE_A")
            assert check is not None
            assert check.device_serial == "DEVICE_A"

            released = mgr.release("DEVICE_A")
            assert released is True

            check_after = mgr.check("DEVICE_A")
            assert check_after is None
        finally:
            mgr.close()

    def test_reserve_idempotent_same_session(self, tmp_path: Path) -> None:
        """Reserving the same device twice from the same session updates it."""
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            r1 = mgr.reserve("DEVICE_A", purpose="first")
            assert isinstance(r1, DeviceReservation)

            r2 = mgr.reserve("DEVICE_A", purpose="second")
            assert isinstance(r2, DeviceReservation)
            assert r2.purpose == "second"
        finally:
            mgr.close()

    def test_release_nonexistent(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            released = mgr.release("NONEXISTENT")
            assert released is False
        finally:
            mgr.close()


class TestReserveConflict:
    def test_reserve_conflict(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        mgr1 = DeviceManager(db_path=db)
        mgr2 = DeviceManager(db_path=db)
        try:
            r1 = mgr1.reserve("DEVICE_A", purpose="session 1 work")
            assert isinstance(r1, DeviceReservation)

            r2 = mgr2.reserve("DEVICE_A", purpose="session 2 work")
            assert isinstance(r2, str)
            assert "held by" in r2
            assert mgr1.session_id in r2  # unowned lease: owner defaults to session id
        finally:
            mgr1.close()
            mgr2.close()


class TestGuard:
    def test_guard_allows_own_reservation(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            mgr.reserve("DEVICE_A")
            assert mgr.guard("DEVICE_A") is None
        finally:
            mgr.close()

    def test_guard_blocks_other_reservation(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        mgr1 = DeviceManager(db_path=db)
        mgr2 = DeviceManager(db_path=db)
        try:
            mgr1.reserve("DEVICE_A", purpose="testing")
            error = mgr2.guard("DEVICE_A")
            assert error is not None
            assert "held by" in error
            assert mgr1.session_id in error  # unowned lease: owner defaults to session id
        finally:
            mgr1.close()
            mgr2.close()

    def test_guard_allows_unreserved(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            assert mgr.guard("DEVICE_A") is None
        finally:
            mgr.close()

    def test_guard_reaps_dead_pid(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        mgr1 = DeviceManager(db_path=db)
        mgr2 = DeviceManager(db_path=db)
        try:
            mgr1.reserve("DEVICE_A", purpose="will die")
            # Simulate dead PID by setting a non-existent PID in the DB
            with mgr1._lock:
                mgr1._conn.execute(
                    "UPDATE reservations SET pid = 999999 WHERE device_serial = ?",
                    ("DEVICE_A",),
                )
                mgr1._conn.commit()
            # Guard from another session should auto-reap and allow access
            assert mgr2.guard("DEVICE_A") is None
            # Reservation should be gone
            assert mgr2.check("DEVICE_A") is None
        finally:
            mgr1.close()
            mgr2.close()


class TestGuardOwnerFence:
    """Audit F5: guard(serial, owner=...) fences a same-session lease by owner."""

    def test_guard_fences_owner_mismatch(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            mgr.reserve("A", owner="lane-a")
            refusal = mgr.guard("A", owner="lane-b")
            assert refusal is not None
            assert "held by lane-a" in refusal
            assert mgr.guard("A", owner="lane-a") is None
            assert mgr.guard("A") is None  # pin: unfenced callers still pass
        finally:
            mgr.close()

    def test_owner_refusal_reuses_held_by_text(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            mgr.reserve("A", purpose="bench", owner="lane-a")
            refusal = mgr.guard("A", owner="lane-b")
            assert refusal is not None
            assert refusal.startswith("Device 'A' is held by lane-a since ")
            assert "(purpose: bench)" in refusal
            assert "another session" not in refusal
            assert mgr.session_id not in refusal
        finally:
            mgr.close()

    def test_guard_unowned_lease_fences_on_session_id(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            mgr.reserve("A")
            refusal = mgr.guard("A", owner="anything")
            assert refusal is not None
            assert f"held by {mgr.session_id}" in refusal
            assert mgr.guard("A", owner=mgr.session_id) is None
            assert mgr.guard("A") is None
        finally:
            mgr.close()

    def test_guard_reaps_before_owner_fence(self, tmp_path: Path) -> None:
        """A stale same-session lease is reaped first — the fence never
        refuses on a lease that no longer exists."""
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            mgr.reserve("A", owner="lane-a")
            with mgr._lock:
                mgr._conn.execute(
                    "UPDATE reservations SET pid = 999999 WHERE device_serial = ?", ("A",)
                )
                mgr._conn.commit()
            assert mgr.guard("A", owner="lane-b") is None
            assert mgr.check("A") is None
        finally:
            mgr.close()

    def test_guard_fences_adopted_lease(self, tmp_path: Path) -> None:
        """#178 adoption re-points a lease to the daemon's session_id, so every
        HTTP lease is same-session from the daemon's view — the fence must hold."""
        db = tmp_path / "test.db"
        holder = DeviceManager(db_path=db)
        holder.reserve("A", owner="lane-a", owner_pid=os.getpid())
        with holder._lock:
            holder._conn.execute(
                "UPDATE reservations SET pid = 999999 WHERE device_serial = ?", ("A",)
            )
            holder._conn.commit()
        daemon = DeviceManager(db_path=db)
        try:
            res = daemon.check("A")
            assert res is not None
            assert res.session_id == daemon.session_id
            refusal = daemon.guard("A", owner="lane-b")
            assert refusal is not None
            assert "held by lane-a" in refusal
            assert daemon.guard("A", owner="lane-a") is None
        finally:
            daemon.close()
            holder.close()

    def test_guard_unfenced_caller_logs_for_distinct_owner(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            mgr.reserve("A", owner="lane-a")
            with patch("linc_nerve.core.device_manager.logger.info") as mock_info:
                assert mgr.guard("A") is None
            mock_info.assert_called_once_with(
                "guard_unfenced_caller", serial="A", held_by="lane-a"
            )
        finally:
            mgr.close()

    def test_guard_unfenced_caller_silent_for_session_owned_lease(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            mgr.reserve("A")
            with patch("linc_nerve.core.device_manager.logger.info") as mock_info:
                assert mgr.guard("A") is None
            mock_info.assert_not_called()
        finally:
            mgr.close()


class TestOwnerMismatch:
    def test_empty_requested_is_never_mismatch(self) -> None:
        assert _owner_mismatch("", "lane-a", "sess-1") is False
        assert _owner_mismatch("", "sess-1", "sess-1") is False

    def test_matching_owner_is_not_mismatch(self) -> None:
        assert _owner_mismatch("lane-a", "lane-a", "sess-1") is False
        assert _owner_mismatch("sess-1", "sess-1", "sess-1") is False

    def test_differing_owner_is_mismatch(self) -> None:
        assert _owner_mismatch("lane-b", "lane-a", "sess-1") is True
        assert _owner_mismatch("anything", "sess-1", "sess-1") is True


class TestReleaseAll:
    def test_release_all(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            mgr.reserve("DEVICE_A")
            mgr.reserve("DEVICE_B")
            mgr.reserve("DEVICE_C")

            count = mgr.release_all()
            assert count == 3

            assert mgr.check("DEVICE_A") is None
            assert mgr.check("DEVICE_B") is None
            assert mgr.check("DEVICE_C") is None
        finally:
            mgr.close()


class TestPool:
    def test_pool_shows_reservations(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        mgr1 = DeviceManager(db_path=db)
        mgr2 = DeviceManager(db_path=db)
        try:
            mgr1.reserve("DEVICE_A", purpose="testing A")
            mgr2.reserve("DEVICE_B", purpose="testing B")

            pool = mgr1.pool()
            assert len(pool) == 2

            serials = {r["device_serial"] for r in pool}
            assert serials == {"DEVICE_A", "DEVICE_B"}

            # Check 'own' flag
            for r in pool:
                if r["device_serial"] == "DEVICE_A":
                    assert r["own"] is True
                    assert r["purpose"] == "testing A"
                else:
                    assert r["own"] is False
        finally:
            mgr1.close()
            mgr2.close()

    def test_pool_empty(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            assert mgr.pool() == []
        finally:
            mgr.close()


class TestReapStale:
    def test_reap_stale_dead_pid(self, tmp_path: Path) -> None:
        """Reaper should clean reservations from dead processes."""
        db = tmp_path / "test.db"
        mgr1 = DeviceManager(db_path=db)
        try:
            mgr1.reserve("DEVICE_A", timeout_seconds=0)

            # Simulate dead PID by updating the reservation to have a stale heartbeat
            # and a PID that doesn't exist
            fake_pid = 99999999  # Extremely unlikely to be a real PID
            with mgr1._lock:
                mgr1._conn.execute(
                    "UPDATE reservations SET pid = ?, last_heartbeat = ? WHERE device_serial = ?",
                    (fake_pid, time.time() - 1000, "DEVICE_A"),
                )
                mgr1._conn.commit()

            # Create a new manager — it reaps stale on init
            mgr2 = DeviceManager(db_path=db)
            try:
                assert mgr2.check("DEVICE_A") is None
            finally:
                mgr2.close()
        finally:
            mgr1.close()


class TestHeartbeat:
    def test_heartbeat_updates(self, tmp_path: Path) -> None:
        """Verify heartbeat updates last_heartbeat."""
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            mgr.reserve("DEVICE_A")
            r1 = mgr.check("DEVICE_A")
            assert r1 is not None
            initial_hb = r1.last_heartbeat

            # Manually trigger a heartbeat update
            with mgr._lock:
                mgr._conn.execute(
                    "UPDATE reservations SET last_heartbeat = ? WHERE session_id = ?",
                    (time.time() + 100, mgr.session_id),
                )
                mgr._conn.commit()

            r2 = mgr.check("DEVICE_A")
            assert r2 is not None
            assert r2.last_heartbeat > initial_hb
        finally:
            mgr.close()


class TestHeartbeatLoopReapResilience:
    """Issue #94 gap 3: ``_heartbeat_loop`` must call ``_reap_stale`` on every
    tick, and a ``_reap_stale`` that raises must not kill the heartbeat thread
    (which would silently disable the stale-reservation self-heal for the
    rest of the process's life).
    """

    @staticmethod
    def _freeze_background_thread(mgr: DeviceManager) -> None:
        """Stop the real daemon thread __init__ started so the test can drive
        ``_heartbeat_loop`` directly, single-threaded, without interference."""
        mgr._heartbeat_stop.set()
        mgr._heartbeat_thread.join(timeout=2)
        mgr._heartbeat_stop.clear()

    def test_heartbeat_loop_calls_reap_stale_every_tick(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            self._freeze_background_thread(mgr)

            call_count = 0

            def fake_reap() -> None:
                nonlocal call_count
                call_count += 1

            mgr._reap_stale = fake_reap  # type: ignore[method-assign]
            # Allow exactly 3 loop-body iterations, then stop.
            mgr._heartbeat_stop.wait = MagicMock(  # type: ignore[method-assign]
                side_effect=[False, False, False, True]
            )

            mgr._heartbeat_loop()

            assert call_count == 3
        finally:
            mgr.close()

    def test_heartbeat_loop_survives_reap_stale_exception(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            self._freeze_background_thread(mgr)

            mgr.reserve("DEVICE_A", purpose="test")
            before = mgr.check("DEVICE_A")
            assert before is not None
            before_hb = before.last_heartbeat

            mgr._reap_stale = MagicMock(  # type: ignore[method-assign]
                side_effect=[Exception("boom"), None, None]
            )
            mgr._heartbeat_stop.wait = MagicMock(  # type: ignore[method-assign]
                side_effect=[False, False, False, True]
            )

            mgr._heartbeat_loop()

            # Survived the raising cycle and kept calling _reap_stale afterward.
            assert mgr._reap_stale.call_count == 3
            after = mgr.check("DEVICE_A")
            assert after is not None
            assert after.last_heartbeat > before_hb
        finally:
            mgr.close()


class TestIsAvailable:
    def test_unreserved_is_available(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            assert mgr.is_available("DEVICE_A") is True
        finally:
            mgr.close()

    def test_own_reservation_is_available(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            mgr.reserve("DEVICE_A")
            assert mgr.is_available("DEVICE_A") is True
        finally:
            mgr.close()

    def test_other_reservation_not_available(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        mgr1 = DeviceManager(db_path=db)
        mgr2 = DeviceManager(db_path=db)
        try:
            mgr1.reserve("DEVICE_A")
            assert mgr2.is_available("DEVICE_A") is False
        finally:
            mgr1.close()
            mgr2.close()


class TestClose:
    def test_close_releases_all(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        mgr1 = DeviceManager(db_path=db)
        mgr1.reserve("DEVICE_A")
        mgr1.reserve("DEVICE_B")
        mgr1.close()

        mgr2 = DeviceManager(db_path=db)
        try:
            assert mgr2.check("DEVICE_A") is None
            assert mgr2.check("DEVICE_B") is None
        finally:
            mgr2.close()


class TestReserveOwner:
    """linc-nerve-enz.3: caller-supplied owner identity so two HTTP lanes on
    the single :8111 daemon process (same DeviceManager session_id) cannot
    silently steal each other's lease."""

    def test_conflicting_owner_blocks_and_leaves_row_untouched(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            r1 = mgr.reserve("DEVICE_A", owner="lane-a")
            assert isinstance(r1, DeviceReservation)

            r2 = mgr.reserve("DEVICE_A", owner="lane-b")
            assert isinstance(r2, str)
            assert "lane-a" in r2

            # Row still belongs to lane-a — untouched by the refused claim.
            check = mgr.check("DEVICE_A")
            assert check is not None
            assert check.owner == "lane-a"
        finally:
            mgr.close()

    def test_same_owner_reserve_is_idempotent(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            r1 = mgr.reserve("DEVICE_A", purpose="first", owner="lane-a")
            assert isinstance(r1, DeviceReservation)

            r2 = mgr.reserve("DEVICE_A", purpose="second", owner="lane-a")
            assert isinstance(r2, DeviceReservation)
            assert r2.purpose == "second"
            assert r2.owner == "lane-a"

            pool = mgr.pool()
            assert len(pool) == 1
        finally:
            mgr.close()

    def test_takeover_reowns_row_and_logs(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            r1 = mgr.reserve("DEVICE_A", owner="lane-a")
            assert isinstance(r1, DeviceReservation)

            r2 = mgr.reserve("DEVICE_A", owner="lane-b", takeover=True)
            assert isinstance(r2, DeviceReservation)
            assert r2.owner == "lane-b"

            pool = mgr.pool()
            assert len(pool) == 1
            assert pool[0]["owner"] == "lane-b"
        finally:
            mgr.close()

    def test_takeover_without_conflict_is_a_normal_reserve(self, tmp_path: Path) -> None:
        """takeover=True on an unreserved device is just a normal reserve."""
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            result = mgr.reserve("DEVICE_A", owner="lane-a", takeover=True)
            assert isinstance(result, DeviceReservation)
            assert result.owner == "lane-a"
        finally:
            mgr.close()


class TestReleaseOwner:
    def test_release_requires_matching_owner(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            mgr.reserve("DEVICE_A", owner="lane-a")
            mgr.reserve("DEVICE_A", owner="lane-b", takeover=True)

            # lane-a no longer owns it — release must refuse and leave it alone.
            assert mgr.release("DEVICE_A", owner="lane-a") is False
            assert mgr.check("DEVICE_A") is not None

            # lane-b (the current owner) can release it.
            assert mgr.release("DEVICE_A", owner="lane-b") is True
            assert mgr.check("DEVICE_A") is None
        finally:
            mgr.close()

    def test_release_default_owner_matches_own_session(self, tmp_path: Path) -> None:
        """No explicit owner == today's semantics: this session's own lease."""
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            mgr.reserve("DEVICE_A")
            assert mgr.release("DEVICE_A") is True
        finally:
            mgr.close()


class TestOwnerColumnMigration:
    def test_old_schema_db_migrates_and_old_row_reads_session_owned(self, tmp_path: Path) -> None:
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            CREATE TABLE reservations (
                device_serial TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                pid INTEGER NOT NULL,
                reserved_at REAL NOT NULL,
                last_heartbeat REAL NOT NULL,
                timeout_seconds INTEGER NOT NULL,
                purpose TEXT NOT NULL DEFAULT ''
            )
            """
        )
        now = time.time()
        conn.execute(
            """
            INSERT INTO reservations
                (device_serial, session_id, pid, reserved_at, last_heartbeat,
                 timeout_seconds, purpose)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("DEVICE_A", "legacy-session", os.getpid(), now, now, 300, "legacy"),
        )
        conn.commit()
        conn.close()

        mgr = DeviceManager(db_path=db)
        try:
            cols = {row[1] for row in mgr._conn.execute("PRAGMA table_info(reservations)")}
            assert "owner" in cols

            raw_owner = mgr._conn.execute(
                "SELECT owner FROM reservations WHERE device_serial = ?", ("DEVICE_A",)
            ).fetchone()[0]
            assert raw_owner == ""  # migrated column defaults empty, not backfilled

            check = mgr.check("DEVICE_A")
            assert check is not None
            assert check.owner == "legacy-session"  # empty owner reads as session-owned

            # Reserving from the same session (today's semantics) is idempotent.
            mgr._session_id = "legacy-session"
            result = mgr.reserve("DEVICE_A", purpose="still working")
            assert isinstance(result, DeviceReservation)
            assert result.purpose == "still working"
        finally:
            mgr.close()
