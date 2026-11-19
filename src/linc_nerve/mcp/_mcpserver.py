"""nerve's MCP server class: a crashed tool still tells the agent why (mcp 2.1+).

From mcp 2.1 the SDK hides the text of any exception other than ``ToolError`` raised in a
tool: the client sees only ``Error executing tool <name>`` (``UnexpectedToolError``), and the
cause stays in the server log. nerve's tools fail on device conditions the agent must read
(device gone, adb refused, timeouts), so ``NerveMCPServer.call_tool`` re-raises such a crash
as a ``ToolError`` carrying ``"<ExceptionClass>: <message>"``. Home directories are shown as
``~`` and the text is capped at ``CLIENT_MSG_MAX`` chars; when ``str()`` itself raises, only
the class name is sent.

A ``ToolError`` the tool raised on purpose, a nested crash and protocol errors pass through
unchanged. The device_tool / global_tool wrappers and their metrics / log_mcp_call
bookkeeping are untouched: this sits above them, on the path every ``tools/call`` takes
(``MCPServer._handle_call_tool`` -> ``call_tool``).
"""

from __future__ import annotations

import os
import re
from typing import Any

from linc_cli_kit import SDKToolError
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import UnexpectedToolError

CLIENT_MSG_MAX = 500
HOME_DIR_RE = re.compile(r"(?:/Users|/home)/[^/\s'\"]+")


class NerveMCPServer(MCPServer):
    """``MCPServer`` whose crashed tool calls surface their cause to the client."""

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        try:
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError as exc:
            cause = exc.__cause__
            if cause is None or isinstance(cause, SDKToolError):
                raise  # no cause, or a nested tool crash: already generic
            try:
                text = f"{type(cause).__name__}: {cause}"
            except Exception:
                text = type(cause).__name__  # str() itself raised: report the class only
            home = os.environ.get("HOME", "").rstrip("/")
            if home:
                text = text.replace(home, "~")
            text = HOME_DIR_RE.sub("~", text)[:CLIENT_MSG_MAX]
            raise SDKToolError(f"Error executing tool {name}: {text}") from cause
