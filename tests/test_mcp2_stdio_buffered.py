"""Unit contracts for ``buffered_stdio_server`` on mcp 2.x (molecule linc-nerve mcp-2x, mcp2-port).

The transport is driven over real OS pipes standing in for stdin / stdout. Invariant under test:
every inbound line yields either a dispatched ``SessionMessage`` or an ``Exception`` item on the
read stream, and the reader never dies on input. The writer must keep a required ``"id": null``
(an error reply to an unparseable request), which ``exclude_none=True`` dropped.
"""

from __future__ import annotations

import json
import os
import types
from typing import Any

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import ErrorData, JSONRPCError, JSONRPCRequest, JSONRPCResponse

import linc_nerve.core.ios  # noqa: F401  (conftest imports ios_discovery; break the import cycle)
from linc_nerve.mcp import stdio_buffered


def _drive(
    monkeypatch: pytest.MonkeyPatch, stdin_bytes: bytes, replies: list[SessionMessage]
) -> tuple[list[SessionMessage | Exception], list[dict[str, Any]]]:
    """Feed *stdin_bytes* then EOF, send *replies*; return read items and written JSON lines."""
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    stdin = os.fdopen(in_r, "rb", buffering=0)
    stdout = os.fdopen(out_w, "wb", buffering=0)
    monkeypatch.setattr(stdio_buffered.sys, "stdin", types.SimpleNamespace(buffer=stdin))
    monkeypatch.setattr(stdio_buffered.sys, "stdout", types.SimpleNamespace(buffer=stdout))
    with os.fdopen(in_w, "wb") as feeder:
        feeder.write(stdin_bytes)

    async def scenario() -> list[SessionMessage | Exception]:
        items: list[SessionMessage | Exception] = []
        async with stdio_buffered.buffered_stdio_server() as (read_stream, write_stream):
            async with read_stream:
                with anyio.fail_after(10):
                    async for item in read_stream:
                        items.append(item)
            async with write_stream:
                for reply in replies:
                    await write_stream.send(reply)
        return items

    try:
        items = anyio.run(scenario)
    finally:
        for f in (stdin, stdout):
            if not f.closed:
                f.close()
    with os.fdopen(out_r, "rb") as drain:
        written = [json.loads(line) for line in drain.read().splitlines() if line.strip()]
    return items, written


def test_reader_yields_messages_and_exceptions_and_writer_keeps_null_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[test] valid request, null-id error response, malformed line -> Msg, Msg, Exception."""
    stdin_bytes = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
        b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Parse error"}}\n'
        b"{not json\n"
    )
    null_id_error = SessionMessage(
        JSONRPCError(jsonrpc="2.0", id=None, error=ErrorData(code=-32700, message="Parse error"))
    )
    ok = SessionMessage(JSONRPCResponse(jsonrpc="2.0", id=1, result={}))
    items, written = _drive(monkeypatch, stdin_bytes, [null_id_error, ok])

    assert [type(i) for i in items[:2]] == [SessionMessage, SessionMessage]
    assert isinstance(items[2], Exception)
    assert len(items) == 3
    first, second = items[0], items[1]
    assert isinstance(first, SessionMessage)
    assert isinstance(first.message, JSONRPCRequest)
    assert isinstance(second, SessionMessage)
    assert isinstance(second.message, JSONRPCError)
    assert second.message.id is None

    assert written[0] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }
    assert written[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param(b"\xff\xfe\xfa{\n", id="invalid-utf8"),
        pytest.param(b"[]\n", id="json-array"),
        pytest.param(b"y" * 4096 + b"\n", id="over-limit-line"),
    ],
)
def test_contract_hostile_line_becomes_exception_item_and_reader_continues(
    monkeypatch: pytest.MonkeyPatch, hostile: bytes
) -> None:
    """[contract] a hostile line yields one Exception item; the next valid line still arrives."""
    monkeypatch.setattr(stdio_buffered, "_MAX_LINE", 1024)
    valid = b'{"jsonrpc":"2.0","id":7,"method":"ping"}\n'
    items, _ = _drive(monkeypatch, b"\n" + hostile + valid, [])

    assert isinstance(items[0], Exception)
    tail = items[-1]
    assert isinstance(tail, SessionMessage)
    assert isinstance(tail.message, JSONRPCRequest)
    assert tail.message.id == 7
    assert all(isinstance(i, Exception) for i in items[:-1])
