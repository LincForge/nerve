"""Buffered stdio transport — drop-in replacement for mcp.server.stdio.

mcp 1.26.0 uses ``anyio.create_memory_object_stream(0)`` (unbuffered
rendezvous channels) for both the read and write paths, creating deadlocks.

This implementation fixes two issues:

1. **Buffered channels** (size=32) decouple reading from writing.
2. **Native asyncio I/O** — uses ``asyncio.StreamReader``/``StreamWriter``
   registered directly with the event loop via ``connect_read_pipe`` /
   ``connect_write_pipe``.  No threads, no TextIOWrapper, no buffering
   layers.  This eliminates the hang-after-2-calls bug caused by
   thread-pool exhaustion or stale thread state in ``anyio.to_thread``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import anyio.lowlevel
import mcp.types as types
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.shared.message import SessionMessage

# Buffer size for the internal channels.  32 is generous for sequential
# tool calls; the important thing is that it's >0.
_BUF = 32

# Debug logging — writes to stderr to avoid corrupting the JSON-RPC stream.
_DEBUG = os.environ.get("NERVE_STDIO_DEBUG", "").lower() in ("1", "true", "yes")


def _dbg(msg: str) -> None:
    if _DEBUG:
        sys.stderr.write(f"[nerve-stdio] {msg}\n")
        sys.stderr.flush()


@asynccontextmanager
async def buffered_stdio_server() -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """Stdio transport with native asyncio I/O and buffered channels.

    Uses asyncio.StreamReader/StreamWriter registered directly with the
    event loop — no threads, no TextIOWrapper, no anyio.wrap_file.
    """
    loop = asyncio.get_running_loop()

    # Connect stdin as an asyncio StreamReader (non-blocking, fd-based).
    stdin_reader = asyncio.StreamReader()
    stdin_protocol = asyncio.StreamReaderProtocol(stdin_reader)
    await loop.connect_read_pipe(lambda: stdin_protocol, sys.stdin.buffer)

    # Connect stdout as an asyncio StreamWriter (non-blocking, fd-based).
    stdout_transport, stdout_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    stdout_writer = asyncio.StreamWriter(
        stdout_transport, stdout_protocol, stdin_reader, loop
    )

    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
    read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]

    write_stream: MemoryObjectSendStream[SessionMessage]
    write_stream_reader: MemoryObjectReceiveStream[SessionMessage]

    read_stream_writer, read_stream = anyio.create_memory_object_stream(_BUF)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(_BUF)

    _msg_counter = {"in": 0, "out": 0}

    async def _stdin_reader_task() -> None:
        """Read JSON-RPC lines from stdin using native asyncio I/O."""
        try:
            async with read_stream_writer:
                while True:
                    line_bytes = await stdin_reader.readline()
                    if not line_bytes:
                        _dbg("stdin  EOF")
                        break

                    line = line_bytes.decode("utf-8").rstrip("\n")
                    if not line:
                        continue

                    _msg_counter["in"] += 1
                    _dbg(f"stdin  #{_msg_counter['in']} len={len(line)}")

                    try:
                        message = types.JSONRPCMessage.model_validate_json(line)
                    except Exception as exc:
                        await read_stream_writer.send(exc)
                        continue

                    session_message = SessionMessage(message)
                    await read_stream_writer.send(session_message)
                    _dbg(f"stdin  #{_msg_counter['in']} dispatched")
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async def _stdout_writer_task() -> None:
        """Write JSON-RPC responses to stdout using native asyncio I/O."""
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    _msg_counter["out"] += 1
                    json_str = session_message.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    payload = (json_str + "\n").encode("utf-8")
                    _dbg(f"stdout #{_msg_counter['out']} len={len(payload)} writing...")
                    stdout_writer.write(payload)
                    await stdout_writer.drain()
                    _dbg(f"stdout #{_msg_counter['out']} done")
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_stdin_reader_task)
        tg.start_soon(_stdout_writer_task)
        yield read_stream, write_stream
