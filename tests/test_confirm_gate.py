"""Tests for the shared destructive-tool confirmation gate (reactor c12557f2).

Covers:
  1. Registry walk — every ``_<name>_sync`` function has a ``confirm``
     parameter (defaulting to False) iff its name is in DESTRUCTIVE_TOOLS.
  2. Each DESTRUCTIVE_TOOLS function actually enforces the gate: a falsy
     confirm never reaches ``_core._resolve_device``; confirm=True proceeds.
  3. MCP tool registration carries ``destructiveHint=True`` for destructive
     tools only.
  4. REVIEWED_NOT_GATED is internally consistent with TOOL_REGISTRY.
  5. No inline gate duplication survives outside _confirm.py.
  6. confirmation_gate() returns a fresh dict every call.
"""

from __future__ import annotations

import asyncio
import glob
import importlib
import inspect
import os
from typing import Any
from unittest.mock import MagicMock

import pytest

from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.ios import IOSController
from linc_nerve.mcp import _core
from linc_nerve.mcp import server as mcp_mod
from linc_nerve.mcp._confirm import DESTRUCTIVE_TOOLS, REVIEWED_NOT_GATED, confirmation_gate


def _resolve_sync_fn(tool_name: str) -> Any:
    """Resolve a registered tool's underlying ``_<name>_sync`` function."""
    entry = _core.TOOL_REGISTRY[tool_name]
    module = importlib.import_module(entry["module"])
    return getattr(module, f"_{tool_name}_sync")


def _reset_singletons() -> None:
    if mcp_mod._device_mgr is not None:
        mcp_mod._device_mgr.close()
    mcp_mod._android = None
    mcp_mod._ios = None
    mcp_mod._state_mgr = None
    mcp_mod._metrics = None
    mcp_mod._device_mgr = None
    mcp_mod._emulator = None
    mcp_mod._cached_devices = []
    mcp_mod._cache_timestamp = 0.0
    mcp_mod._cleanup_done = False


def _phone() -> NerveDevice:
    return NerveDevice(
        serial="PIXEL8PRO",
        platform=Platform.ANDROID,
        device_type=DeviceType.PHONE,
        model="Pixel 8 Pro",
        os_version="14",
    )


# Call args (excluding confirm) for each DESTRUCTIVE_TOOLS function. Kept
# explicit per-tool because the underlying signatures differ (uninstall/
# clear_app_data take package; grant_permission also takes a permission).
_CALL_ARGS: dict[str, tuple[Any, ...]] = {
    "nerve_uninstall": ("PIXEL8PRO", "com.app"),
    "nerve_clear_app_data": ("PIXEL8PRO", "com.app"),
    "nerve_grant_permission": ("PIXEL8PRO", "com.app", "android.permission.CAMERA"),
}

_CONTROLLER_METHOD: dict[str, str] = {
    "nerve_uninstall": "uninstall",
    "nerve_clear_app_data": "clear_app_data",
    "nerve_grant_permission": "grant_permission",
}

_CONFIRM_IDS = ["omitted", "False", "str-false", "int-1"]


class TestRegistryWalk:
    """Acceptance criterion 1: confirm param presence tracks DESTRUCTIVE_TOOLS."""

    def test_confirm_param_matches_destructive_tools_registry(self) -> None:
        assert _core.TOOL_REGISTRY, "tool registry must be populated by import side effects"
        for name in _core.TOOL_REGISTRY:
            fn = _resolve_sync_fn(name)
            params = inspect.signature(fn).parameters
            has_confirm = "confirm" in params
            assert has_confirm == (name in DESTRUCTIVE_TOOLS), (
                f"{name}: has_confirm={has_confirm}, in DESTRUCTIVE_TOOLS="
                f"{name in DESTRUCTIVE_TOOLS}"
            )
            if has_confirm:
                assert params["confirm"].default is False, name


class TestDestructiveToolsEnforceGate:
    """Acceptance criterion 2: the gate actually blocks/proceeds."""

    def _setup(self, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        _reset_singletons()
        mock_android = MagicMock(spec=AndroidController)
        mock_android.discover.return_value = [_phone()]
        mock_ios = MagicMock(spec=IOSController)
        mock_ios.discover.return_value = []
        mcp_mod._android = mock_android
        mcp_mod._ios = mock_ios
        return mock_android

    @pytest.mark.parametrize("tool_name", sorted(DESTRUCTIVE_TOOLS))
    @pytest.mark.parametrize("confirm_value", [None, False, "false", 1], ids=_CONFIRM_IDS)
    def test_falsy_confirm_never_reaches_resolve_device(
        self, tool_name: str, confirm_value: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._setup(monkeypatch)

        def _boom(serial: str) -> Any:
            raise AssertionError("gate did not short-circuit before device resolution")

        monkeypatch.setattr(_core, "_resolve_device", _boom)

        fn = _resolve_sync_fn(tool_name)
        args = _CALL_ARGS[tool_name]
        result = fn(*args) if confirm_value is None else fn(*args, confirm=confirm_value)

        assert result == {"success": False, "error": "confirmation_required"}

    @pytest.mark.parametrize("tool_name", sorted(DESTRUCTIVE_TOOLS))
    def test_confirm_true_proceeds(self, tool_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_android = self._setup(monkeypatch)

        fn = _resolve_sync_fn(tool_name)
        args = _CALL_ARGS[tool_name]
        result = fn(*args, confirm=True)

        assert result["success"] is True
        method = getattr(mock_android, _CONTROLLER_METHOD[tool_name])
        method.assert_called_once()


class TestDestructiveHintAnnotations:
    """Acceptance criterion 3: MCP tool registration carries destructiveHint."""

    def test_destructive_tools_have_destructive_hint(self) -> None:
        tools = asyncio.run(_core.server.list_tools())
        by_name = {t.name: t for t in tools}

        for tool_name in DESTRUCTIVE_TOOLS:
            tool = by_name[tool_name]
            assert tool.annotations is not None, tool_name
            assert tool.annotations.destructiveHint is True, tool_name

        for tool_name in ("nerve_tap", "nerve_screenshot"):
            tool = by_name[tool_name]
            has_hint = tool.annotations is not None and tool.annotations.destructiveHint is True
            assert not has_hint, tool_name


class TestReviewedNotGated:
    """Acceptance criterion 4: REVIEWED_NOT_GATED stays consistent with TOOL_REGISTRY."""

    def test_keys_present_in_registry(self) -> None:
        for name in REVIEWED_NOT_GATED:
            assert name in _core.TOOL_REGISTRY, name

    def test_disjoint_from_destructive_tools(self) -> None:
        assert set(REVIEWED_NOT_GATED).isdisjoint(DESTRUCTIVE_TOOLS)

    def test_reasons_non_empty(self) -> None:
        for name, reason in REVIEWED_NOT_GATED.items():
            assert isinstance(reason, str) and reason.strip(), name


class TestNoInlineGateDuplication:
    """Acceptance criterion 5: the inline gate pattern only lives in _confirm.py."""

    def test_no_stray_inline_gate(self) -> None:
        mcp_dir = os.path.dirname(mcp_mod.__file__) if hasattr(mcp_mod, "__file__") else None
        if mcp_dir is None:
            mcp_dir = os.path.dirname(importlib.import_module("linc_nerve.mcp._core").__file__)
        offenders = []
        for path in glob.glob(os.path.join(mcp_dir, "**", "*.py"), recursive=True):
            if os.path.basename(path) == "_confirm.py":
                continue
            with open(path, encoding="utf-8") as f:
                if "confirm is not True" in f.read():
                    offenders.append(path)
        assert offenders == []


class TestConfirmationGateHelper:
    """Acceptance criterion 6: fresh dict per call."""

    def test_returns_new_dict_each_call(self) -> None:
        first = confirmation_gate(False)
        second = confirmation_gate(False)
        assert first == second
        assert first is not second

        first["mutated"] = "oops"  # type: ignore[index]
        assert "mutated" not in second

    def test_true_returns_none(self) -> None:
        assert confirmation_gate(True) is None
