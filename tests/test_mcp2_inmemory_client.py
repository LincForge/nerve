"""In-memory MCP round trip on mcp 2.x (molecule linc-nerve mcp-2x, mcp2-port).

Connects the SDK's ``mcp.Client`` to nerve's ``MCPServer`` in-process and runs tools/list +
tools/call, so the server-side wiring (strict_tools, async wrappers, result conversion) is
exercised through a real client session without a subprocess. No device is touched:
``nerve_session_stats`` is a global tool that reads only in-memory state.
"""

from __future__ import annotations

import json

import anyio
import pytest
from mcp import Client

from linc_nerve.cli.main import _registry_server
from linc_nerve.mcp import _core

pytestmark = pytest.mark.usefixtures("restore_signal_handlers")


def test_inmemory_tools_list_and_call_roundtrip() -> None:
    """[test] tools/list matches the registry; a call returns JSON text; an unknown arg errors."""
    server = _registry_server()

    async def scenario() -> tuple[set[str], str, bool, str]:
        async with Client(server) as client:
            listed = await client.list_tools()
            ok = await client.call_tool("nerve_session_stats", {})
            bad = await client.call_tool("nerve_session_stats", {"unknown_arg_xyz": 1})
        assert ok.is_error is False
        return (
            {t.name for t in listed.tools},
            ok.content[0].text,  # type: ignore[union-attr]
            bad.is_error,
            bad.content[0].text,  # type: ignore[union-attr]
        )

    names, ok_text, bad_is_error, bad_text = anyio.run(scenario)
    assert names == set(_core.TOOL_REGISTRY)
    assert "count" in json.loads(ok_text)
    assert bad_is_error is True
    assert "unknown_arg_xyz" in bad_text
