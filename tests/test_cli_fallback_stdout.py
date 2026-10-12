"""Regression: the local-fallback path must never put structlog output on stdout.

``linc_nerve.mcp.server`` is the only module that points structlog at stderr, and the CLI
deliberately never imports it (it installs SIGTERM/SIGINT handlers + a faulthandler at
import time). Unconfigured structlog's ``PrintLoggerFactory`` writes to **stdout**, so
without the module-level ``structlog.configure(...)`` at the top of
``linc_nerve/cli/main.py`` the fallback path's ``tool_registry_built``
(``mcp/sse_server.py``) and ``device_manager_init`` (``core/device_manager.py``) lines land
in front of the JSON payload and break ``nerve <cmd> | jq`` on every host with no running
nerve daemon (e.g. the MacBook). Reproduced before the fix as:

    $ LINC_NERVE_DAEMON=http://127.0.0.1:59999 nerve capabilities | jq .tool_count
    jq: parse error: Invalid numeric literal at line 1, column 11

This runs in a SUBPROCESS rather than through ``CliRunner``. ``PrintLoggerFactory(file=...)``
captures the stream *object* at configure time, and modules that bind a logger at import
time (``mcp/sse_server.py``) resolve the factory right then — so a CliRunner that swaps
``sys.stdout``/``sys.stderr`` afterwards is invisible to structlog, and an in-process test
cannot observe which stream the log line actually reached. A real process can. (This test
is also what proved the configure call had to move ABOVE the ``linc_nerve`` imports: placed
after them it still leaked, because sse_server had already bound its logger.)
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.usefixtures("restore_signal_handlers")

# Forces the HTTP leg to fail (connection refused) so LocalTransport runs, then stubs the
# resolved tool callable — but calls the REAL _get_tool_registry() first, so its
# log.info("tool_registry_built", ...) fires exactly as it does in production.
_SCRIPT = textwrap.dedent(
    """
    import sys

    import linc_nerve.cli.main as m
    from linc_nerve.mcp.sse_server import _get_tool_registry


    def _stub_local_fn(spec):
        _get_tool_registry()  # real build -> emits tool_registry_built
        return lambda **payload: {"ok": True}


    m.TRANSPORT.fallback._fn_for = _stub_local_fn
    m.TRANSPORT.fallback._before = lambda: None  # no real device init
    sys.argv = ["nerve", "capabilities"]
    m.main()
    """
)


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_local_fallback_keeps_structlog_off_stdout() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **__import__("os").environ,
            "LINC_NERVE_DAEMON": f"http://127.0.0.1:{_closed_port()}",
        },
    )

    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert json.loads(proc.stdout) == {"ok": True}, f"stdout polluted: {proc.stdout!r}"
    assert "tool_registry_built" not in proc.stdout
    assert "tool_registry_built" in proc.stderr
