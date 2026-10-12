"""DbC unit 4: component-initialised checks survive ``python -O``.

Bare ``assert`` is stripped under ``-O``, so every "this component exists"
check in shipped code goes through ``require()`` (or an explicit raise) and
fails with a ``RuntimeError`` naming the missing component — never an
``AssertionError`` and never a silent ``AttributeError`` on ``None``.
"""

from __future__ import annotations

from typing import Any

import pytest

from linc_nerve.core.contracts import require
from linc_nerve.core.wda import WDASession
from linc_nerve.mcp import _core
from linc_nerve.mcp.diagnostic_tools import _nerve_metrics_sync
from linc_nerve.mcp.discovery_tools import _nerve_pool_sync
from linc_nerve.mcp.emulator_tools import (
    _nerve_emulator_list_sync,
    _nerve_emulator_start_sync,
    _nerve_emulator_stop_sync,
    _nerve_inject_sensor_sync,
)
from linc_nerve.mcp.reservation_tools import _nerve_release_sync, _nerve_reserve_sync

# ---------------------------------------------------------------------------
# require() helper
# ---------------------------------------------------------------------------


def test_require_none_raises_runtime_error_naming_component() -> None:
    with pytest.raises(RuntimeError, match=r"^x is not initialised$"):
        require(None, "x")


@pytest.mark.parametrize("value", [0, "", [], False, object()])
def test_require_returns_value_unchanged(value: Any) -> None:
    # Falsy-but-present values are initialised: only None trips the contract.
    assert require(value, "x") is value


# ---------------------------------------------------------------------------
# MCP tool paths — _core singleton missing after _ensure_initialized()
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every _core singleton is None and _ensure_initialized() is a no-op.

    Models an initialiser that "succeeded" without producing the component a
    tool needs — the exact state the bare asserts used to guard.
    """
    for name in ("_android", "_ios", "_state_mgr", "_metrics", "_device_mgr", "_emulator"):
        monkeypatch.setattr(_core, name, None)
    monkeypatch.setattr(_core, "_ensure_initialized", lambda: (None, None))


@pytest.mark.usefixtures("empty_core")
@pytest.mark.parametrize(
    ("call", "component"),
    [
        (lambda: _nerve_reserve_sync("SERIAL"), "device manager"),
        (lambda: _nerve_release_sync("SERIAL"), "device manager"),
        (_nerve_pool_sync, "device manager"),
        (_nerve_metrics_sync, "metrics collector"),
        (_core._get_state_mgr, "state session manager"),
        (_nerve_emulator_list_sync, "emulator manager"),
        (lambda: _nerve_emulator_start_sync("Pixel_8"), "emulator manager"),
        (lambda: _nerve_emulator_stop_sync("emulator-5554"), "emulator manager"),
        (lambda: _nerve_inject_sensor_sync("emulator-5554"), "Android controller"),
    ],
)
def test_mcp_paths_raise_runtime_error_when_component_missing(call: Any, component: str) -> None:
    with pytest.raises(RuntimeError, match=f"{component} is not initialised"):
        call()


@pytest.mark.usefixtures("empty_core")
def test_emulator_tools_name_missing_ios_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_core, "_emulator", object())
    with pytest.raises(RuntimeError, match="iOS controller is not initialised"):
        _nerve_emulator_list_sync()


@pytest.mark.parametrize(
    ("android", "component"),
    [(None, "Android controller"), (object(), "iOS controller")],
)
def test_ensure_initialized_raises_when_init_leaves_controller_unset(
    monkeypatch: pytest.MonkeyPatch, android: Any, component: str
) -> None:
    monkeypatch.setattr(_core, "_android", android)
    monkeypatch.setattr(_core, "_ios", None)
    monkeypatch.setattr(_core, "_init_singletons", lambda: None)
    with pytest.raises(RuntimeError, match=f"{component} is not initialised"):
        _core._ensure_initialized()


# ---------------------------------------------------------------------------
# Core path — WDASession with no HTTP client
# ---------------------------------------------------------------------------


@pytest.fixture
def clientless_session(monkeypatch: pytest.MonkeyPatch) -> WDASession:
    """A WDASession whose start/stop/session-create never produce a client."""
    session = WDASession("SIM-UDID", 8100)
    monkeypatch.setattr(session, "start", lambda: None)
    monkeypatch.setattr(session, "stop", lambda: None)
    monkeypatch.setattr(session, "_create_session", lambda: None)
    session._client = None
    return session


def test_wda_request_without_client_raises_runtime_error(clientless_session: WDASession) -> None:
    with pytest.raises(RuntimeError, match="WDA client is not initialised"):
        clientless_session._request("GET", "/status")


def test_wda_session_retry_without_client_raises_runtime_error(
    clientless_session: WDASession,
) -> None:
    with pytest.raises(RuntimeError, match="WDA client is not initialised"):
        clientless_session._retry_with_new_session("GET", "/status")
