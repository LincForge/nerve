"""pool() rows and guard() refusals name the holder (reactor 0e3ca14f)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from linc_nerve.core.device_manager import DeviceManager

TIMEOUT = 300


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _mgr(tmp_path: Path, name: str = "t.db") -> DeviceManager:
    mgr = DeviceManager(db_path=tmp_path / name)
    mgr._heartbeat_stop.set()
    mgr._heartbeat_thread.join(timeout=2)
    return mgr


def _row(mgr: DeviceManager) -> dict:
    rows = mgr.pool()
    assert len(rows) == 1
    return rows[0]


def test_held_by_names_owner_time_and_unspecified_purpose(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    try:
        mgr.reserve("D1", owner="andre-imagination-bqx1", purpose="", timeout_seconds=TIMEOUT)
        row = _row(mgr)
        iso = datetime.fromtimestamp(row["reserved_at"], tz=UTC).isoformat()
        assert (
            row["held_by"] == f"held by andre-imagination-bqx1 since {iso} (purpose: unspecified)"
        )
    finally:
        mgr.close()


def test_held_by_includes_purpose(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    try:
        mgr.reserve("D1", owner="o", purpose="nightly", timeout_seconds=TIMEOUT)
        assert _row(mgr)["held_by"].endswith("(purpose: nightly)")
    finally:
        mgr.close()


def test_owner_alive_states_and_expires_in(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    try:
        mgr.reserve("D1", owner="o", timeout_seconds=TIMEOUT)
        row = _row(mgr)
        assert row["owner_pid"] is None
        assert row["owner_alive"] is None
        assert abs(row["expires_in_s"] - (TIMEOUT - row["seconds_since_heartbeat"])) <= 1
        mgr.reserve("D1", owner="o", timeout_seconds=TIMEOUT, owner_pid=os.getpid())
        row = _row(mgr)
        assert row["owner_pid"] == os.getpid()
        assert row["owner_alive"] is True
        dead = _dead_pid()
        mgr._conn.execute("UPDATE reservations SET owner_pid = ?", (dead,))
        mgr._conn.commit()
        row = _row(mgr)
        assert row["owner_pid"] == dead
        assert row["owner_alive"] is False
    finally:
        mgr.close()


def test_expires_in_negative_when_stale(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    try:
        mgr.reserve("D1", owner="o", timeout_seconds=10)
        mgr._conn.execute("UPDATE reservations SET last_heartbeat = ?", (time.time() - 60,))
        mgr._conn.commit()
        assert _row(mgr)["expires_in_s"] < 0
    finally:
        mgr.close()


def test_guard_names_owner_not_session(tmp_path: Path) -> None:
    holder = _mgr(tmp_path)
    other = _mgr(tmp_path)
    try:
        holder.reserve("D1", owner="seat-x", timeout_seconds=TIMEOUT)
        # keep the lease live: holder is this process, heartbeat fresh
        msg = other.guard("D1")
        assert msg is not None
        assert "held by seat-x" in msg
        assert holder.session_id not in msg
        assert msg.endswith("Use nerve_pool to see all reservations.")
        assert msg.startswith("Device 'D1' is held by seat-x since ")
    finally:
        holder.close()
        other.close()


def test_pool_preexisting_keys_unchanged(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    try:
        mgr.reserve("D1", owner="seat-x", purpose="p", timeout_seconds=TIMEOUT)
        row = _row(mgr)
        for key in (
            "device_serial",
            "session_id",
            "pid",
            "reserved_at",
            "age_seconds",
            "last_heartbeat",
            "seconds_since_heartbeat",
            "timeout_seconds",
            "purpose",
            "owner",
            "own",
        ):
            assert key in row
        assert row["owner"] == "seat-x"
        assert row["session_id"] == mgr.session_id
        assert row["purpose"] == "p"
        assert row["timeout_seconds"] == TIMEOUT
        assert row["own"] is True
    finally:
        mgr.close()


def test_reserve_conflict_message_uses_held_by(tmp_path: Path) -> None:
    a = _mgr(tmp_path)
    b = _mgr(tmp_path)
    try:
        a.reserve("D1", owner="seat-x", timeout_seconds=TIMEOUT)
        res = b.reserve("D1", owner="seat-y", timeout_seconds=TIMEOUT)
        assert isinstance(res, str)
        assert "held by seat-x" in res
    finally:
        a.close()
        b.close()
