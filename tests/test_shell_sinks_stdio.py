"""Tests for shell sink validation on stdio/MCP transports."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from linc_nerve.mcp.diagnostic_tools import _inspect_android


def test_inspect_android_rejects_hostile_package() -> None:
    ctrl = MagicMock()
    result: dict = {}
    with pytest.raises(ValueError):
        _inspect_android(ctrl, "ABC", "com.x;touch /sdcard/pwned", 50, result)
    ctrl._shell.assert_not_called()
