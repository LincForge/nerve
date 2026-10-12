"""Owner-bound lease liveness (reactor 0e3ca14f, the 2026-09-16 nightly).

The daemon heartbeat renews only leases whose holder is provably alive: rows
the manager holds for itself, and rows whose ``owner_pid`` is set and alive.
A caller-owned lease with no owner_pid expires after ``timeout_seconds``
unless the caller re-reserves.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

from linc_nerve.core.device_manager import DeviceManager

TIMEOUT = 300


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _freeze(mgr: DeviceManager) -> None:
    mgr._heartbeat_stop.set()
    mgr._heartbeat_thread.join(timeout=2)
    mgr._heartbeat_stop.clear()


def _one_tick(mgr: DeviceManager) -> None:
    mgr._heartbeat_stop.wait = MagicMock(side_effect=[False, True])  # type: ignore[method-assign]
    mgr._heartbeat_loop()


def _age(mgr: DeviceManager, serial: str, seconds: float) -> float:
    ts = time.time() - seconds
    mgr._conn.execute(
        "UPDATE reservations SET last_heartbeat = ? WHERE device_serial = ?", (ts, serial)
    )
    mgr._conn.commit()
    return ts


def _hb(mgr: DeviceManager, serial: str) -> float:
    res = mgr.check(serial)
    assert res is not None
    return res.last_heartbeat


def _mgr(tmp_path: Path) -> DeviceManager:
    mgr = DeviceManager(db_path=tmp_path / "t.db")
    _freeze(mgr)
    return mgr


def test_caller_owned_lease_not_refreshed_by_tick(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    try:
        mgr.reserve("D1", owner="seat-x", timeout_seconds=TIMEOUT)
        ts = _age(mgr, "D1", 10)
        _one_tick(mgr)
        assert _hb(mgr, "D1") == ts
    finally:
        mgr.close()


def test_caller_owned_lease_expires_though_pid_alive(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    try:
        mgr.reserve("D1", owner="seat-x", timeout_seconds=TIMEOUT)
        _one_tick(mgr)
        _age(mgr, "D1", TIMEOUT + 1)
        mgr._reap_stale()
        assert mgr.check("D1") is None
    finally:
        mgr.close()


def test_same_owner_reserve_renews(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    try:
        mgr.reserve("D1", owner="seat-x", timeout_seconds=TIMEOUT)
        _age(mgr, "D1", TIMEOUT - 5)
        assert not isinstance(mgr.reserve("D1", owner="seat-x", timeout_seconds=TIMEOUT), str)
        assert time.time() - _hb(mgr, "D1") < 5
        mgr._reap_stale()
        assert mgr.check("D1") is not None
    finally:
        mgr.close()


def test_dead_owner_pid_reaped_with_fresh_heartbeat(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    try:
        mgr.reserve("D1", owner="seat-x", owner_pid=_dead_pid())
        assert mgr.check("D1") is not None
        mgr._reap_stale()
        assert mgr.check("D1") is None
    finally:
        mgr.close()


def test_live_owner_pid_keeps_lease(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    try:
        mgr.reserve("D1", owner="seat-x", owner_pid=os.getpid(), timeout_seconds=TIMEOUT)
        _age(mgr, "D1", TIMEOUT + 1)
        _one_tick(mgr)
        assert time.time() - _hb(mgr, "D1") < 5
        mgr._reap_stale()
        assert mgr.check("D1") is not None
    finally:
        mgr.close()


def test_guard_reaps_dead_owner_pid(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    try:
        mgr.reserve("D1", owner="seat-x", owner_pid=_dead_pid())
        assert mgr.guard("D1") is None
        assert mgr.check("D1") is None
    finally:
        mgr.close()


def test_owner_pid_set_on_same_owner_update_and_takeover(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    try:
        mgr.reserve("D1", owner="a")
        mgr.reserve("D1", owner="a", owner_pid=os.getpid())
        assert _stored_owner_pid(mgr) == os.getpid()
        mgr.reserve("D1", owner="b", takeover=True, owner_pid=0)
        assert _stored_owner_pid(mgr) is None
        mgr.reserve("D1", owner="c", takeover=True, owner_pid=os.getpid())
        assert _stored_owner_pid(mgr) == os.getpid()
    finally:
        mgr.close()


def _stored_owner_pid(mgr: DeviceManager) -> int | None:
    row = mgr._conn.execute("SELECT owner_pid FROM reservations").fetchone()
    return row[0]


def test_in_process_lease_still_refreshed_and_reaped(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    try:
        mgr.reserve("D1", timeout_seconds=TIMEOUT)
        _age(mgr, "D1", 100)
        _one_tick(mgr)
        assert time.time() - _hb(mgr, "D1") < 5
        mgr._conn.execute("UPDATE reservations SET pid = ?", (_dead_pid(),))
        mgr._conn.commit()
        mgr._reap_stale()
        assert mgr.check("D1") is None
    finally:
        mgr.close()


def test_migrates_db_without_owner_pid(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE reservations (
            device_serial TEXT PRIMARY KEY, session_id TEXT NOT NULL, pid INTEGER NOT NULL,
            reserved_at REAL NOT NULL, last_heartbeat REAL NOT NULL,
            timeout_seconds INTEGER NOT NULL, purpose TEXT NOT NULL DEFAULT '',
            owner TEXT NOT NULL DEFAULT '')"""
    )
    now = time.time()
    conn.execute(
        "INSERT INTO reservations VALUES ('OLD', 'sess', ?, ?, ?, 300, 'p', 'o')",
        (os.getpid(), now, now),
    )
    conn.commit()
    conn.close()
    mgr = DeviceManager(db_path=db)
    try:
        cols = {r[1] for r in mgr._conn.execute("PRAGMA table_info(reservations)")}
        assert "owner_pid" in cols
        assert mgr.check("OLD") is not None
    finally:
        mgr.close()


# --- F6: a live owner_pid outlives the daemon that stored the lease ---------


def test_live_owner_pid_decides_over_dead_storing_pid() -> None:
    from linc_nerve.core.device_manager import _lease_stale_reason

    assert _lease_stale_reason(_dead_pid(), os.getpid(), 1, TIMEOUT) is None


def test_dead_owner_pid_is_owner_gone_regardless_of_storing_pid() -> None:
    from linc_nerve.core.device_manager import _lease_stale_reason

    dead = _dead_pid()
    assert _lease_stale_reason(os.getpid(), dead, 1, TIMEOUT) == "owner_gone"
    assert _lease_stale_reason(_dead_pid(), dead, 1, TIMEOUT) == "owner_gone"


def test_no_owner_pid_dead_storing_pid_is_dead_pid() -> None:
    from linc_nerve.core.device_manager import _lease_stale_reason

    assert _lease_stale_reason(_dead_pid(), None, 1, TIMEOUT) == "dead_pid"


def test_live_owner_pid_still_times_out() -> None:
    from linc_nerve.core.device_manager import _lease_stale_reason

    assert _lease_stale_reason(_dead_pid(), os.getpid(), TIMEOUT + 1, TIMEOUT) == (
        "heartbeat_timeout"
    )


def _orphan(mgr: DeviceManager, serial: str, owner_pid: int) -> None:
    """Reserve ``serial`` with an owner_pid, then rewrite the storing pid to a
    dead one — the state a restarted daemon finds on disk."""
    mgr.reserve(serial, owner="seat-x", owner_pid=owner_pid, timeout_seconds=TIMEOUT)
    mgr._conn.execute(
        "UPDATE reservations SET pid = ? WHERE device_serial = ?", (_dead_pid(), serial)
    )
    mgr._conn.commit()


def test_new_manager_adopts_live_owner_lease(tmp_path: Path) -> None:
    mgr1 = _mgr(tmp_path)
    _orphan(mgr1, "A", os.getpid())
    mgr2 = _mgr(tmp_path)
    try:
        res = mgr2.check("A")
        assert res is not None
        assert res.session_id == mgr2.session_id
        assert res.pid == os.getpid()
        assert mgr2.guard("A") is None
        assert mgr2.pool()[0]["owner_alive"] is True
    finally:
        mgr2.close()
        mgr1.close()


def test_new_manager_reaps_dead_owner_lease_not_adopts(
    tmp_path: Path, monkeypatch: object
) -> None:
    import linc_nerve.core.device_manager as dm

    mgr1 = _mgr(tmp_path)
    _orphan(mgr1, "A", _dead_pid())
    log = MagicMock()
    monkeypatch.setattr(dm, "logger", log)  # type: ignore[attr-defined]
    mgr2 = _mgr(tmp_path)
    try:
        assert mgr2.check("A") is None
        events = {c.args[0]: c.kwargs for c in log.info.call_args_list}
        assert "lease_adopted" not in events
        assert events["reaping_stale_reservation"]["reason"] == "owner_gone"
    finally:
        mgr2.close()
        mgr1.close()


def test_adoption_logs_owner(tmp_path: Path, monkeypatch: object) -> None:
    import linc_nerve.core.device_manager as dm

    mgr1 = _mgr(tmp_path)
    _orphan(mgr1, "A", os.getpid())
    log = MagicMock()
    monkeypatch.setattr(dm, "logger", log)  # type: ignore[attr-defined]
    mgr2 = _mgr(tmp_path)
    try:
        events = {c.args[0]: c.kwargs for c in log.info.call_args_list}
        assert events["lease_adopted"] == {
            "serial": "A",
            "owner": "seat-x",
            "owner_pid": os.getpid(),
            "session_id": mgr2.session_id,
        }
    finally:
        mgr2.close()
        mgr1.close()


def test_guard_should_reap_false_for_live_owner_dead_storing_pid(tmp_path: Path) -> None:
    other = _mgr(tmp_path)
    holder = _mgr(tmp_path)
    try:
        _orphan(holder, "A", os.getpid())
        res = other.check("A")
        assert res is not None
        assert other._guard_should_reap(res) is False
    finally:
        holder.close()
        other.close()


def test_guard_logs_reap_reason(tmp_path: Path, monkeypatch: object) -> None:
    import linc_nerve.core.device_manager as dm

    other = _mgr(tmp_path)
    holder = _mgr(tmp_path)
    try:
        holder.reserve("A", owner="seat-x", owner_pid=_dead_pid())
        log = MagicMock()
        monkeypatch.setattr(dm, "logger", log)  # type: ignore[attr-defined]
        assert other.guard("A") is None
        events = {c.args[0]: c.kwargs for c in log.info.call_args_list}
        assert events["guard_reaping_dead_reservation"]["reason"] == "owner_gone"
    finally:
        holder.close()
        other.close()


def test_release_all_keeps_live_owner_lease(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    mgr = _mgr(tmp_path)
    mgr.reserve("A", owner="seat-x", owner_pid=os.getpid())
    mgr.reserve("B")
    assert mgr.release_all() == 1
    assert mgr.check("A") is not None
    assert mgr.check("B") is None
    mgr.close()
    fresh = DeviceManager(db_path=db)
    try:
        assert fresh.check("A") is not None
    finally:
        fresh.close()


def test_release_all_drops_dead_owner_lease(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    try:
        mgr.reserve("A", owner="seat-x", owner_pid=_dead_pid())
        assert mgr.release_all() == 1
        assert mgr.check("A") is None
    finally:
        mgr.close()
