"""Unit tests for ``_ready_adb_serials`` — the raw ``host:devices`` read.

Plain unit tests with a fake adb client: no device, no repo-file reads, so they
run inside a mutation sandbox too.
"""

from __future__ import annotations

from typing import Any

import pytest

# conftest's autouse Xcode fixture imports ios_discovery, which only resolves its
# ios <-> ios_discovery cycle when ios is imported first.
import linc_nerve.core.ios  # noqa: F401
from linc_nerve.core.android import _ready_adb_serials


class _FakeConn:
    def __init__(self, output: str, events: list[str], *, okay_error: Exception | None = None):
        self._output = output
        self._events = events
        self._okay_error = okay_error

    def __enter__(self) -> _FakeConn:
        self._events.append("enter")
        return self

    def __exit__(self, *exc: Any) -> None:
        self._events.append("exit")

    def send_command(self, cmd: str) -> None:
        self._events.append(f"send:{cmd}")

    def check_okay(self) -> None:
        self._events.append("okay")
        if self._okay_error is not None:
            raise self._okay_error

    def read_string_block(self) -> str:
        self._events.append("read")
        return self._output


class _FakeClient:
    def __init__(self, output: str = "", *, okay_error: Exception | None = None):
        self.events: list[str] = []
        self._output = output
        self._okay_error = okay_error

    def make_connection(self) -> _FakeConn:
        return _FakeConn(self._output, self.events, okay_error=self._okay_error)


def test_ready_adb_serials_speaks_host_devices_then_reads_the_block() -> None:
    client = _FakeClient("FAKESERIAL01\tdevice\n")

    assert _ready_adb_serials(client) == ("FAKESERIAL01",)
    assert client.events == ["enter", "send:host:devices", "okay", "read", "exit"]


def test_ready_adb_serials_keeps_a_spaced_mdns_serial_intact() -> None:
    spaced = "adb-FAKESERIAL01-x (2)._adb-tls-connect._tcp"
    client = _FakeClient(f"{spaced}\tdevice\nFAKESERIAL02\tdevice\n")

    assert _ready_adb_serials(client) == (spaced, "FAKESERIAL02")


def test_ready_adb_serials_drops_offline_and_unauthorized_devices() -> None:
    client = _FakeClient(
        "FAKESERIAL01\toffline\nFAKESERIAL02\tunauthorized\nFAKESERIAL03\tdevice\n"
    )

    assert _ready_adb_serials(client) == ("FAKESERIAL03",)


def test_ready_adb_serials_returns_empty_tuple_for_an_empty_fleet() -> None:
    assert _ready_adb_serials(_FakeClient("")) == ()


def test_ready_adb_serials_propagates_a_failed_okay_without_reading() -> None:
    client = _FakeClient("FAKESERIAL01\tdevice\n", okay_error=RuntimeError("FAIL"))

    with pytest.raises(RuntimeError, match="FAIL"):
        _ready_adb_serials(client)
    assert "read" not in client.events
    assert client.events[-1] == "exit"
