"""Tests for server.py → _core singleton delegation.

The server module must transparently delegate singleton reads AND writes
to _core, so that test monkey-patching via ``mcp_mod._android = mock``
is visible to all tool modules that read ``_core._android``.

TDD RED: these tests fail because module __setattr__ doesn't work.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from linc_nerve.core.android import AndroidController
from linc_nerve.core.ios import IOSController
from linc_nerve.mcp import _core
from linc_nerve.mcp import server as mcp_mod


class TestServerDelegation:
    """server.py must delegate singleton attribute access to _core."""

    def test_write_delegates_to_core(self) -> None:
        """Setting mcp_mod._android = X must set _core._android = X."""
        original = _core._android
        try:
            sentinel = MagicMock(spec=AndroidController)
            mcp_mod._android = sentinel
            assert _core._android is sentinel, (
                "Write delegation broken: mcp_mod._android = X did not propagate to _core._android"
            )
        finally:
            _core._android = original

    def test_read_delegates_from_core(self) -> None:
        """Reading mcp_mod._android must return _core._android."""
        original = _core._android
        try:
            sentinel = MagicMock(spec=AndroidController)
            _core._android = sentinel
            assert mcp_mod._android is sentinel
        finally:
            _core._android = original

    def test_write_then_read_roundtrip(self) -> None:
        """Write via mcp_mod, read back via mcp_mod — must see same value."""
        original = _core._android
        try:
            sentinel = MagicMock(spec=IOSController)
            mcp_mod._ios = sentinel
            assert mcp_mod._ios is sentinel
            assert _core._ios is sentinel
        finally:
            _core._android = original

    def test_non_delegated_attr_stays_on_server(self) -> None:
        """Attributes NOT in the delegation set stay on the server module."""
        mcp_mod._test_local_attr = "local_value"
        assert mcp_mod._test_local_attr == "local_value"
        assert not hasattr(_core, "_test_local_attr")
        del mcp_mod.__dict__["_test_local_attr"]

    def test_all_singletons_delegate(self) -> None:
        """Every singleton in _DELEGATED_TO_CORE must delegate writes."""
        singletons = ["_android", "_ios", "_state_mgr", "_metrics", "_device_mgr", "_emulator"]
        originals = {name: getattr(_core, name) for name in singletons}
        try:
            for name in singletons:
                sentinel = f"SENTINEL_{name}"
                setattr(mcp_mod, name, sentinel)
                assert getattr(_core, name) == sentinel, (
                    f"Delegation broken for {name}: "
                    f"setattr(mcp_mod, {name!r}, ...) did not propagate to _core"
                )
        finally:
            for name, val in originals.items():
                setattr(_core, name, val)

    def test_tool_registry_parity(self) -> None:
        """Assert that the stdio-registered tool set == the HTTP tool set."""
        from linc_nerve.mcp._core import server
        from linc_nerve.mcp.sse_server import _get_tool_registry

        # Assert on the _TOOL_MODULES single source of truth by comparing
        # the actually registered tools on both stdio and HTTP sides.
        stdio_tool_names = {t.name for t in server._tool_manager.list_tools()}
        http_tool_names = set(_get_tool_registry().keys())

        assert stdio_tool_names == http_tool_names, (
            f"Mismatch between FastMCP stdio tools and HTTP daemon tools!\n"
            f"Stdio only: {stdio_tool_names - http_tool_names}\n"
            f"HTTP only: {http_tool_names - stdio_tool_names}"
        )
