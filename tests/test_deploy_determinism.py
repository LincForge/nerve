"""Deploy-determinism guards (#79).

Two coupled footguns bit us live (nerve #37/#38):
1. A tool module registered via @device_tool/@global_tool but absent from
   sse_server._TOOL_MODULES 404s over :8111 with no error at merge time.
2. The long-lived :8111 daemon serves pre-merge code until kickstarted.

These tests make failure mode 1 impossible to merge: _TOOL_MODULES is derived
from the package contents on disk, and any module carrying registrations must
be picked up by that derivation. All unit-level — no daemon, no device.
"""

from __future__ import annotations

import pathlib
import re

import pytest

import linc_nerve.mcp
import linc_nerve.mcp.sse_server as sse

_MCP_PKG_DIR = pathlib.Path(linc_nerve.mcp.__file__).parent

# Matches a decorator application of the registration decorators, whether
# imported bare (`@device_tool`) or namespaced (`@_core.device_tool`).
_REGISTRATION_RE = re.compile(r"^\s*@(?:[\w.]+\.)?(?:device_tool|global_tool)\b", re.MULTILINE)


def _disk_tool_modules() -> list[str]:
    """All linc_nerve.mcp.*_tools modules present on disk, sorted."""
    return sorted(
        f"linc_nerve.mcp.{p.stem}" for p in _MCP_PKG_DIR.glob("*_tools.py") if p.stem != "__init__"
    )


def test_tool_modules_is_derived_from_disk():
    """_TOOL_MODULES must equal the *_tools.py modules actually in the package.

    This is the auto-derivation AC: adding a new mcp/<name>_tools.py file must
    make it reachable over :8111 (and imported by server.py) with zero manual
    list edits — a hardcoded list that drifts from disk fails here.
    """
    assert _disk_tool_modules() == sse._TOOL_MODULES


def test_tool_modules_nonempty_and_contains_known_modules():
    """Sanity: derivation found the real modules (guards an empty-glob bug)."""
    assert len(sse._TOOL_MODULES) >= 10
    assert "linc_nerve.mcp.state_tools" in sse._TOOL_MODULES
    assert "linc_nerve.mcp.app_tools" in sse._TOOL_MODULES


def test_every_registration_module_is_in_tool_modules():
    """Any mcp/*.py module using @device_tool/@global_tool must be in _TOOL_MODULES.

    Catches the near-miss the *_tools.py glob can't: a registrations module
    named without the `_tools` suffix would import-register on stdio (if
    imported) but be invisible to the :8111 registry — the exact #37/#38
    dual-registration failure. Source-text scan, no imports needed.
    """
    offenders = []
    for path in _MCP_PKG_DIR.glob("*.py"):
        if path.name in ("_core.py", "__init__.py"):
            continue  # _core defines the decorators; skip definition site
        module = f"linc_nerve.mcp.{path.stem}"
        if _REGISTRATION_RE.search(path.read_text()) and module not in sse._TOOL_MODULES:
            offenders.append(module)
    assert not offenders, (
        f"Modules with @device_tool/@global_tool registrations missing from "
        f"_TOOL_MODULES (unreachable over :8111): {offenders}. Rename to "
        f"<name>_tools.py or fix the derivation in sse_server.py."
    )


def test_registration_parity_every_stdio_tool_reachable_over_http():
    """Every @device_tool/@global_tool registration is in the sse tool table.

    Unit-level: imports the modules (registration happens at import time via
    the decorators) and compares the FastMCP tool table against the HTTP
    registry. No daemon needed.
    """
    import importlib

    for module_path in sse._TOOL_MODULES:
        importlib.import_module(module_path)

    from linc_nerve.mcp._core import server

    sse._TOOL_REGISTRY = None  # force rebuild
    http_tools = set(sse._get_tool_registry().keys())
    stdio_tools = {t.name for t in server._tool_manager.list_tools()}

    missing = stdio_tools - http_tools
    assert not missing, f"Registered on stdio but NOT reachable over :8111: {missing}"


def test_dead_code_removed() -> None:
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("linc_nerve.daemon." + "apk" + "_cache")

    import linc_nerve.core.device
    from linc_nerve.core.android import AndroidController

    assert hasattr(AndroidController, "screenshot_b64") is False
    assert "Device" not in linc_nerve.core.device.__all__
