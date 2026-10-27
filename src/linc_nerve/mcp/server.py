"""MCP server for linc-nerve — unified device control.

Start with ``nerve bellows``. Configure in ``~/.claude.json``.
"""

from __future__ import annotations

import faulthandler
import os
import signal
import sys
import types
from typing import IO, Any

from linc_nerve.core.observability import linc_home

_THREAD_DUMP_ENV = "NERVE_THREAD_DUMP"


def _thread_dump_file() -> IO[str]:
    """Return the faulthandler target: stderr, or a private file when opted in.

    Nothing is opened in a shared temp dir (security review L3). With
    ``NERVE_THREAD_DUMP=1`` dumps go to ``$LINC_HOME/nerve/nerve_threads.txt``,
    created owner-only and never through a pre-planted symlink.
    """
    if not os.getenv(_THREAD_DUMP_ENV):
        return sys.stderr
    path = linc_home() / "nerve" / "nerve_threads.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    return os.fdopen(os.open(path, flags, 0o600), "w")


# Dump all thread tracebacks on SIGUSR1: ``kill -USR1 <pid>`` → _thread_dump_file()
_faulthandler_file = _thread_dump_file()
faulthandler.enable(file=_faulthandler_file)
if hasattr(signal, "SIGUSR1"):
    faulthandler.register(signal.SIGUSR1, file=_faulthandler_file, all_threads=True)

# CRITICAL: structlog to stderr — stdout is the JSON-RPC transport for MCP. The sink
# follows sys.stderr at write time (linc_nerve.live_stderr).
from linc_nerve.live_stderr import configure_structlog_to_stderr  # noqa: E402

configure_structlog_to_stderr()

import importlib  # noqa: E402

from linc_nerve.mcp import _core  # noqa: E402
from linc_nerve.mcp.sse_server import _TOOL_MODULES  # noqa: E402

# Import tool modules — registration happens at import time via decorators
for module_path in _TOOL_MODULES:
    importlib.import_module(module_path)


def _sigterm_handler(signum: int, frame: Any) -> None:
    """Clean up scrcpy streams on SIGTERM, then exit."""
    _core._shutdown_cleanup()
    sys.exit(0)


signal.signal(signal.SIGTERM, _sigterm_handler)
signal.signal(signal.SIGINT, _sigterm_handler)

# Re-export server instance for cli/main.py
server = _core.server

# Re-export _sync functions for test backward compat
from linc_nerve.mcp.app_tools import (  # noqa: E402,F401
    _nerve_apps_sync,
    _nerve_clear_app_data_sync,
    _nerve_grant_permission_sync,
    _nerve_install_sync,
    _nerve_kill_sync,
    _nerve_launch_sync,
    _nerve_uninstall_sync,
)
from linc_nerve.mcp.device_state_tools import (  # noqa: E402,F401
    _nerve_device_state_sync,
)
from linc_nerve.mcp.diagnostic_tools import (  # noqa: E402,F401
    _nerve_logcat_sync,
    _nerve_metrics_sync,
    _nerve_session_stats_sync,
    _nerve_stream_logs_sync,
    _nerve_usage_stats_sync,
)
from linc_nerve.mcp.discovery_tools import (  # noqa: E402,F401
    _nerve_capabilities_sync,
    _nerve_discover_sync,
    _nerve_pool_sync,
)
from linc_nerve.mcp.emulator_tools import (  # noqa: E402,F401
    _nerve_emulator_list_sync,
    _nerve_emulator_start_sync,
    _nerve_emulator_stop_sync,
    _nerve_inject_sensor_sync,
)
from linc_nerve.mcp.input_tools import (  # noqa: E402,F401
    _nerve_density_sync,
    _nerve_key_sync,
    _nerve_long_press_sync,
    _nerve_swipe_sync,
    _nerve_tap_sync,
    _nerve_type_sync,
)
from linc_nerve.mcp.navigation_tools import (  # noqa: E402,F401
    _nerve_navigate_screenshot_sync,
    _nerve_navigate_sync,
)
from linc_nerve.mcp.recording_tools import (  # noqa: E402,F401
    _nerve_extract_frames_sync,
    _nerve_record_fixed_sync,
    _nerve_record_start_sync,
    _nerve_record_stop_sync,
)
from linc_nerve.mcp.reservation_tools import (  # noqa: E402,F401
    _nerve_release_sync,
    _nerve_reserve_sync,
)
from linc_nerve.mcp.screenshot_tools import (  # noqa: E402,F401
    _nerve_screenshot_sync,
    _nerve_stream_screenshot_sync,
    _nerve_stream_start_sync,
    _nerve_stream_status_sync,
    _nerve_stream_stop_sync,
)
from linc_nerve.mcp.ui_tools import (  # noqa: E402,F401
    _nerve_diff_sync,
    _nerve_elements_sync,
    _nerve_state_sync,
)

# Delegate singleton access to _core for test monkey-patching.
# When tests do ``mcp_mod._android = mock``, the write must propagate to
# ``_core._android`` so all tool modules (which read from _core) see it.
#
# PEP 562 only supports module __getattr__ — Python does NOT call a
# module-level __setattr__ for normal attribute assignment. To make both
# reads and writes delegate, we swap this module's class to a custom
# ModuleType subclass whose __setattr__/__getattr__ ARE called by Python.
_DELEGATED_TO_CORE = {
    "_android",
    "_ios",
    "_state_mgr",
    "_metrics",
    "_device_mgr",
    "_emulator",
    "_daemon_mode",
    "set_daemon_mode",
    "_cached_devices",
    "_cache_timestamp",
    "_cleanup_done",
    "_device_semaphores",
    "_CACHE_TTL",
    "_invalidate_cache",
    "_init_lock",
    "_cache_lock",
    "_run_on_device",
    "_shutdown_cleanup",
    "_ensure_initialized",
    "_get_state_mgr",
    "_refresh_cache",
    "_get_cached_devices",
    "_resolve_device",
    "_get_controller",
    "_check_guard",
    "_next_output_path",
}


class _DelegatingModule(types.ModuleType):
    """Module subclass that delegates singleton access to _core."""

    def __getattr__(self, name: str) -> Any:
        if name in _DELEGATED_TO_CORE:
            return getattr(_core, name)
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _DELEGATED_TO_CORE:
            setattr(_core, name, value)
        else:
            super().__setattr__(name, value)


sys.modules[__name__].__class__ = _DelegatingModule
