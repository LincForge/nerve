"""Tests for the nerve daemon's port-conflict probe.

The daemon command should mirror bellows' behavior: if a healthy nerve daemon
already owns :8111, exit cleanly (success) so launchd can ThrottleInterval-respawn
without filling logs with OSError tracebacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_daemon_skips_bind_when_healthy_peer_owns_port() -> None:
    """If _probe_existing_daemon returns True, never call asyncio.start_server."""
    from linc_nerve.cli import main as cli_main

    with (
        patch.object(cli_main, "_probe_existing_daemon", new=AsyncMock(return_value=True)),
        patch.object(
            cli_main.asyncio,
            "start_server",
            new=AsyncMock(side_effect=AssertionError("must not bind")),
        ),
    ):
        result = await cli_main.run_daemon_async(port=8111)

    assert result is False, "expected False to signal 'deferred, no work done'"


@pytest.mark.asyncio
async def test_daemon_binds_when_port_is_free() -> None:
    """If _probe_existing_daemon returns False, run normally and bind the server.

    We don't actually want the poll loop running in the test, so we stub it.
    The loop.add_signal_handler call is also stubbed — it would otherwise leak
    signal-handler state into sibling tests that introspect SIGINT/SIGTERM.
    """
    from linc_nerve.cli import main as cli_main

    fake_server = MagicMock()
    fake_server.close = MagicMock()
    fake_server.serve_forever = AsyncMock(return_value=None)
    fake_server.__aenter__ = AsyncMock(return_value=fake_server)
    fake_server.__aexit__ = AsyncMock(return_value=None)

    # Stub loop.add_signal_handler so SIGINT/SIGTERM stay untouched in this
    # process — otherwise pytest-asyncio's per-test loop cleanup overwrites
    # the SIGINT handler with default_int_handler and pollutes test_mcp's
    # introspection of mcp_mod._sigterm_handler.
    loop = asyncio.get_event_loop()
    mock_start_server = AsyncMock(return_value=fake_server)
    with (
        patch.object(cli_main, "_probe_existing_daemon", new=AsyncMock(return_value=False)),
        patch.object(cli_main.asyncio, "start_server", new=mock_start_server),
        patch.object(cli_main, "_run_poll_loop", new=AsyncMock(return_value=None)),
        # The transport watchdog is the daemon's other long-lived background
        # task (linc-nerve#131). Like the poll loop it would otherwise keep the
        # TaskGroup open for its full 30s tick and time this test out.
        patch.object(cli_main, "_run_adb_watchdog", new=AsyncMock(return_value=None)),
        patch.object(cli_main, "AndroidController", new=MagicMock()),
        patch.object(cli_main, "IOSController", new=MagicMock()),
        patch.object(cli_main, "DaemonWDAManager", new=MagicMock()),
        patch.object(cli_main, "_ensure_initialized", new=MagicMock()),
        patch.object(loop, "add_signal_handler", new=MagicMock()),
    ):
        result = await asyncio.wait_for(cli_main.run_daemon_async(port=8111), timeout=2.0)
        mock_start_server.assert_called_once()

    assert result is True, "expected True to signal 'served'"


@pytest.mark.asyncio
async def test_poll_loop_does_not_block_event_loop() -> None:
    """A slow synchronous discover() in the poll cycle must not freeze the loop.

    The poll/WDA-reconcile cycle calls controller.discover() (an adb/simctl
    subprocess) on every 30s tick. If that runs inline on the event loop, a slow
    cold discovery freezes /health for its whole duration (improvement 4244d1fe:
    a single ~9s /health stall observed live, aligned with the poll cycle).

    Racing a concurrent liveness probe against the blocking cycle is what proves
    the loop is free: with the off-loop fix the probe completes on its own ~0.2s
    schedule; without it the frozen loop cannot advance the probe's sleep until
    discover() returns. The race is essential — a non-raced probe measured after
    the block cleared would falsely pass (same gotcha as the #25 offload test).
    """
    from linc_nerve.cli import main as cli_main
    from linc_nerve.mcp import sse_server

    sse_server._shutdown_tool_executor()
    block_secs = 1.2

    class _SlowController:
        """android.discover() blocks; the loop must not block with it."""

        def discover(self) -> list:
            time.sleep(block_secs)
            return []

    class _FastController:
        def discover(self) -> list:
            return []

    class _NoopWDA:
        def reconcile(self, ios_devices: object) -> None:
            pass

    stop_event = asyncio.Event()
    t0 = time.monotonic()
    poll = asyncio.create_task(
        cli_main._run_poll_loop(_SlowController(), _FastController(), _NoopWDA(), stop_event)
    )

    # Concurrent liveness probe — must complete on its own ~0.2s schedule, not
    # be held hostage by the blocking discover() running in the poll cycle.
    await asyncio.sleep(0.2)
    liveness_t = time.monotonic() - t0

    stop_event.set()
    poll.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await poll
    sse_server._shutdown_tool_executor()

    assert liveness_t < block_secs - 0.5, (
        f"liveness probe completed at {liveness_t:.2f}s (block={block_secs}s) — "
        "the poll cycle's synchronous discover() blocked the event loop"
    )


@pytest.mark.asyncio
async def test_daemon_starts_the_adb_transport_watchdog() -> None:
    """The daemon must actually launch the watchdog it depends on for /health.

    Without this task /health has no reading to render and silently reports
    `pending` forever — which is precisely the unconditional-ok behaviour that
    let a 28-hour bench outage go unnoticed (#131).
    """
    from linc_nerve.cli import main as cli_main

    fake_server = MagicMock()
    fake_server.close = MagicMock()
    fake_server.serve_forever = AsyncMock(return_value=None)
    fake_server.__aenter__ = AsyncMock(return_value=fake_server)
    fake_server.__aexit__ = AsyncMock(return_value=None)

    loop = asyncio.get_event_loop()
    mock_watchdog = AsyncMock(return_value=None)
    with (
        patch.object(cli_main, "_probe_existing_daemon", new=AsyncMock(return_value=False)),
        patch.object(cli_main.asyncio, "start_server", new=AsyncMock(return_value=fake_server)),
        patch.object(cli_main, "_run_poll_loop", new=AsyncMock(return_value=None)),
        patch.object(cli_main, "_run_adb_watchdog", new=mock_watchdog),
        patch.object(cli_main, "AndroidController", new=MagicMock()),
        patch.object(cli_main, "IOSController", new=MagicMock()),
        patch.object(cli_main, "DaemonWDAManager", new=MagicMock()),
        patch.object(cli_main, "_ensure_initialized", new=MagicMock()),
        patch.object(loop, "add_signal_handler", new=MagicMock()),
    ):
        await asyncio.wait_for(cli_main.run_daemon_async(port=8111), timeout=2.0)

    mock_watchdog.assert_called_once()


@pytest.mark.asyncio
async def test_adb_watchdog_registers_itself_and_ticks_off_the_event_loop() -> None:
    """The watchdog must mark the process monitored and run its tick in a thread.

    Running on the daemon's shared single-worker tool executor would queue it
    behind the very call a wedged transport blocks — it would go silent exactly
    when it is needed.
    """
    from linc_nerve.cli import main as cli_main
    from linc_nerve.core import adb_health

    adb_health.reset_transport_status()
    try:
        stop_event = asyncio.Event()
        ticks: list[str] = []

        def _tick(**kwargs: object) -> object:
            ticks.append(threading.current_thread().name)
            stop_event.set()
            return None

        with patch.object(adb_health, "watchdog_tick", new=_tick):
            await asyncio.wait_for(
                cli_main._run_adb_watchdog(MagicMock(), stop_event, interval=0.01),
                timeout=5.0,
            )

        assert adb_health.watchdog_uptime_seconds() is not None, "must mark itself monitored"
        assert ticks, "watchdog never ticked"
        assert ticks[0] != threading.current_thread().name, "tick must run off the event loop"
    finally:
        adb_health.reset_transport_status()


@pytest.mark.asyncio
async def test_adb_watchdog_survives_a_tick_that_raises() -> None:
    """A watchdog that can die is a watchdog that stops watching."""
    from linc_nerve.cli import main as cli_main
    from linc_nerve.core import adb_health

    adb_health.reset_transport_status()
    try:
        stop_event = asyncio.Event()
        calls = {"n": 0}

        def _tick(**kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] >= 2:
                stop_event.set()
            raise RuntimeError("probe exploded")

        with patch.object(adb_health, "watchdog_tick", new=_tick):
            await asyncio.wait_for(
                cli_main._run_adb_watchdog(MagicMock(), stop_event, interval=0.01),
                timeout=5.0,
            )

        assert calls["n"] >= 2, "loop must keep ticking after a raising tick"
    finally:
        adb_health.reset_transport_status()
