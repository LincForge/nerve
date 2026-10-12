import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from linc_nerve.mcp.sse_server import start_sse_server


@pytest.mark.asyncio
async def test_sse_e2e_stream_logs(unused_tcp_port: int) -> None:
    # 1. Start the SSE server in the background
    server_task = asyncio.create_task(start_sse_server(host="127.0.0.1", port=unused_tcp_port))
    await asyncio.sleep(0.1)  # Allow server to bind

    # 2. Mock device and controller to simulate ADB/simctl subprocess
    mock_device = MagicMock()
    mock_ctrl = MagicMock()
    mock_ctrl.get_log_stream_cmd.return_value = ["echo", "log1\nError2\nlog3"]

    with (
        patch("linc_nerve.mcp.sse_server._resolve_device", return_value=(mock_device, "android")),
        patch("linc_nerve.mcp.sse_server._get_controller", return_value=mock_ctrl),
    ):
        # Mock the subprocess
        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_stdout = asyncio.StreamReader()
        mock_stdout.feed_data(b"log1\nException2\nlog3\n")
        mock_stdout.feed_eof()
        mock_proc.stdout = mock_stdout

        async def mock_exec(*args: Any, **kwargs: Any) -> Any:
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            # 3. Connect via httpx stream
            events = []
            async with (
                httpx.AsyncClient() as client,
                client.stream(
                    "GET", f"http://127.0.0.1:{unused_tcp_port}/stream?device=ABC&filter=com.app"
                ) as response,
            ):
                assert response.status_code == 200
                assert response.headers["Content-Type"] == "text/event-stream"

                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))

            assert len(events) == 3
            assert events[0] == {"event": "log", "data": "log1"}
            assert events[1] == {"event": "log_error", "data": "Exception2"}
            assert events[2] == {"event": "log", "data": "log3"}

    # Shutdown server
    server_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await server_task
