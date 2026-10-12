"""Contract tests for DeviceManager reservation API.

Pin the public API surface — field names, types, method signatures, and DB
schema — so breaking changes fail fast in CI.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

from linc_nerve.core.device_manager import DeviceManager, DeviceReservation


class TestDeviceReservationFieldsPinned:
    """AC-1: DeviceReservation fields pinned (8 fields, exact names+types).

    linc-nerve-enz.3 added ``owner`` (caller identity, distinct from the
    per-process ``session_id``) so HTTP lanes sharing one daemon session
    cannot silently steal each other's lease.
    """

    def test_exactly_eight_fields(self) -> None:
        fields = dataclasses.fields(DeviceReservation)
        assert len(fields) == 8

    def test_field_names_and_types(self) -> None:
        fields = dataclasses.fields(DeviceReservation)
        actual = [(f.name, f.type) for f in fields]
        expected = [
            ("device_serial", "str"),
            ("session_id", "str"),
            ("pid", "int"),
            ("reserved_at", "float"),
            ("last_heartbeat", "float"),
            ("timeout_seconds", "int"),
            ("purpose", "str"),
            ("owner", "str"),
        ]
        assert actual == expected


class TestDeviceManagerMethodSignaturesPinned:
    """AC-2: DeviceManager public method signatures pinned."""

    def test_reserve_signature(self) -> None:
        sig = inspect.signature(DeviceManager.reserve)
        params = list(sig.parameters.keys())
        assert params == [
            "self",
            "serial",
            "purpose",
            "timeout_seconds",
            "owner",
            "takeover",
            "owner_pid",
        ]
        # Check defaults
        assert sig.parameters["serial"].default is inspect.Parameter.empty
        assert sig.parameters["purpose"].default == ""
        assert sig.parameters["timeout_seconds"].default == 300
        assert sig.parameters["owner"].default == ""
        assert sig.parameters["takeover"].default is False
        assert sig.parameters["owner_pid"].default == 0
        # Check annotations
        assert sig.parameters["serial"].annotation == "str"
        assert sig.parameters["purpose"].annotation == "str"
        assert sig.parameters["timeout_seconds"].annotation == "int"
        assert sig.parameters["owner"].annotation == "str"
        assert sig.parameters["takeover"].annotation == "bool"
        # owner/takeover are keyword-only
        assert sig.parameters["owner"].kind == inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["takeover"].kind == inspect.Parameter.KEYWORD_ONLY
        # Check return type
        assert sig.return_annotation == "DeviceReservation | str"

    def test_release_signature(self) -> None:
        sig = inspect.signature(DeviceManager.release)
        params = list(sig.parameters.keys())
        assert params == ["self", "serial", "owner"]
        assert sig.parameters["serial"].annotation == "str"
        assert sig.parameters["owner"].annotation == "str"
        assert sig.parameters["owner"].default == ""
        assert sig.parameters["owner"].kind == inspect.Parameter.KEYWORD_ONLY
        assert sig.return_annotation == "bool"

    def test_release_all_signature(self) -> None:
        sig = inspect.signature(DeviceManager.release_all)
        params = list(sig.parameters.keys())
        assert params == ["self"]
        assert sig.return_annotation == "int"


class TestReservationsTableSchemaPinned:
    """AC-3: SQLite reservations table schema pinned (8 columns)."""

    def test_table_schema(self, tmp_path: Path) -> None:
        mgr = DeviceManager(db_path=tmp_path / "test.db")
        try:
            rows = mgr._conn.execute("PRAGMA table_info(reservations)").fetchall()
            # PRAGMA table_info returns: (cid, name, type, notnull, dflt_value, pk)
            schema = [(r[1], r[2], r[3], r[4], r[5]) for r in rows]
            expected = [
                ("device_serial", "TEXT", 0, None, 1),
                ("session_id", "TEXT", 1, None, 0),
                ("pid", "INTEGER", 1, None, 0),
                ("reserved_at", "REAL", 1, None, 0),
                ("last_heartbeat", "REAL", 1, None, 0),
                ("timeout_seconds", "INTEGER", 1, None, 0),
                ("purpose", "TEXT", 1, "''", 0),
                ("owner", "TEXT", 1, "''", 0),
                ("owner_pid", "INTEGER", 0, None, 0),
            ]
            assert schema == expected
            assert len(rows) == 9
        finally:
            mgr.close()
