"""M1 (2026-10-02 pre-release security review): emulator-console command
injection through ``nerve_inject_sensor``'s ``sensor_type``.

``inject_sensor`` used to interpolate ``sensor_type`` straight into
``f"sensor set {sensor_type} {x}:{y}:{z}\\n"`` on the AUTHENTICATED emulator
telnet console, so ``"acceleration\\nkill"`` ran a second console verb with
the user's console token. These tests pin: the allowlist at the core sink,
the tool layer, and the :8111 router pre-dispatch guard — all with recording
fakes (no real sockets, no real token file).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from linc_nerve.core.android import AndroidController
from linc_nerve.daemon import http_router
from linc_nerve.daemon.http_router import route_request

_TOKEN = "FAKE-TOKEN"
_AUTH_LINE = f"auth {_TOKEN}\n".encode()

_HOSTILE_SENSOR_TYPES = [
    "acceleration\nkill",
    "acceleration\r\nscreenrecord start /tmp/x",
    "acceleration 0:0:0\nkill",
    "acceleration\r",
    "\nkill",
    "ACCELERATION",
    "acceleration ",
    "",
    "not-a-sensor",
]


class _RecordingConsoleSocket:
    """Fake emulator console: answers ``OK`` to everything and records every
    byte the client sends."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.connected_to: Any = None

    def settimeout(self, _timeout: float) -> None:
        pass

    def connect(self, addr: Any) -> None:
        self.connected_to = addr

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, _bufsize: int) -> bytes:
        return b"OK\r\n"

    def close(self) -> None:
        pass


def _inject(sensor_type: Any, x: Any = 0, y: Any = 0, z: Any = 0) -> tuple[Any, list[bytes]]:
    sock = _RecordingConsoleSocket()
    with (
        patch("linc_nerve.core.android._socket_mod.socket", return_value=sock),
        patch.object(AndroidController, "_read_auth_token", return_value=_TOKEN),
    ):
        result = AndroidController().inject_sensor("emulator-5554", sensor_type, x, y, z)
    return result, sock.sent


def _beyond_auth(sent: list[bytes]) -> list[bytes]:
    """Everything the console received other than auth + the cleanup quit."""
    return [chunk for chunk in sent if chunk not in (_AUTH_LINE, b"quit\n")]


# ---------------------------------------------------------------------------
# Core sink — AndroidController.inject_sensor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sensor_type", ["acceleration\nkill", "acceleration\r\nscreenrecord start /tmp/x"]
)
def test_inject_sensor_rejects_newline_injection(sensor_type: str) -> None:
    """The review's exploit shapes raise ValueError and the console receives
    nothing beyond auth."""
    sock = _RecordingConsoleSocket()
    with (
        patch("linc_nerve.core.android._socket_mod.socket", return_value=sock),
        patch.object(AndroidController, "_read_auth_token", return_value=_TOKEN),
        pytest.raises(ValueError, match="sensor_type"),
    ):
        AndroidController().inject_sensor("emulator-5554", sensor_type, 0, 0, 0)
    assert _beyond_auth(sock.sent) == []


@pytest.mark.parametrize("sensor_type", _HOSTILE_SENSOR_TYPES)
def test_inject_sensor_rejects_every_non_allowlisted_name(sensor_type: str) -> None:
    sock = _RecordingConsoleSocket()
    with (
        patch("linc_nerve.core.android._socket_mod.socket", return_value=sock),
        patch.object(AndroidController, "_read_auth_token", return_value=_TOKEN),
        pytest.raises(ValueError),
    ):
        AndroidController().inject_sensor("emulator-5554", sensor_type, 0, 0, 0)
    assert _beyond_auth(sock.sent) == []


def test_inject_sensor_legitimate_sensor_sends_exact_command() -> None:
    result, sent = _inject("acceleration", 0, 0, 0)
    assert result["success"] is True
    assert sent == [_AUTH_LINE, b"sensor set acceleration 0:0:0\n", b"quit\n"]


def test_inject_sensor_keeps_float_formatting() -> None:
    result, sent = _inject("magnetic-field", 1.5, -2.3, 9.81)
    assert result["success"] is True
    assert b"sensor set magnetic-field 1.5:-2.3:9.81\n" in sent


@pytest.mark.parametrize("field", ["x", "y", "z"])
def test_inject_sensor_rejects_non_numeric_axis(field: str) -> None:
    """A library caller bypassing the router's float coercion can't smuggle a
    console line through an axis value either."""
    axes: dict[str, Any] = {"x": 0, "y": 0, "z": 0}
    axes[field] = "0\nkill"
    sock = _RecordingConsoleSocket()
    with (
        patch("linc_nerve.core.android._socket_mod.socket", return_value=sock),
        patch.object(AndroidController, "_read_auth_token", return_value=_TOKEN),
        pytest.raises(ValueError, match=field),
    ):
        AndroidController().inject_sensor("emulator-5554", "acceleration", **axes)
    assert _beyond_auth(sock.sent) == []


@pytest.mark.parametrize("token", ["tok\nkill", "tok\rkill"])
def test_inject_sensor_rejects_newline_in_auth_token(token: str) -> None:
    sock = _RecordingConsoleSocket()
    with (
        patch("linc_nerve.core.android._socket_mod.socket", return_value=sock),
        patch.object(AndroidController, "_read_auth_token", return_value=token),
        pytest.raises(ValueError, match="token"),
    ):
        AndroidController().inject_sensor("emulator-5554", "acceleration", 0, 0, 0)
    assert all(b"kill" not in chunk for chunk in sock.sent)


# ---------------------------------------------------------------------------
# Pure helpers — core/emulator_console.py
# ---------------------------------------------------------------------------


@pytest.fixture
def emulator_console() -> Any:
    from linc_nerve.core import emulator_console as module

    return module


def test_documented_sensor_names_are_allowlisted(emulator_console: Any) -> None:
    for name in (
        "acceleration",
        "gyroscope",
        "magnetic-field",
        "orientation",
        "temperature",
        "proximity",
    ):
        assert emulator_console.is_allowed_sensor_type(name)


@pytest.mark.parametrize("value", [*_HOSTILE_SENSOR_TYPES, None, 1, ["acceleration"]])
def test_is_allowed_sensor_type_rejects(emulator_console: Any, value: Any) -> None:
    assert emulator_console.is_allowed_sensor_type(value) is False


def test_validate_sensor_type_returns_value(emulator_console: Any) -> None:
    assert emulator_console.validate_sensor_type("gyroscope") == "gyroscope"


# ---------------------------------------------------------------------------
# Tool layer — nerve_inject_sensor
# ---------------------------------------------------------------------------


def test_tool_rejects_hostile_sensor_type_before_controller() -> None:
    from linc_nerve.mcp import _core
    from linc_nerve.mcp import server as mcp_mod

    mock_android = MagicMock(spec=AndroidController)
    with (
        patch.object(_core, "_ensure_initialized"),
        patch.object(_core, "_android", mock_android),
        pytest.raises(ValueError, match="sensor_type"),
    ):
        mcp_mod._nerve_inject_sensor_sync("emulator-5554", sensor_type="acceleration\nkill")
    mock_android.inject_sensor.assert_not_called()


# ---------------------------------------------------------------------------
# :8111 router pre-dispatch guard
# ---------------------------------------------------------------------------


def _fake_inject_registry(calls: list[str]) -> dict[str, Any]:
    def fake_inject(
        device: str = "",
        sensor_type: str = "acceleration",
        x: float = 0.0,
        y: float = 0.0,
        z: float = 9.81,
    ) -> dict:
        calls.append(sensor_type)
        return {"success": True}

    return {"nerve_inject_sensor": fake_inject}


@pytest.mark.parametrize("sensor_type", [*_HOSTILE_SENSOR_TYPES, 7, None])
def test_route_rejects_bad_sensor_type_before_dispatch(sensor_type: Any) -> None:
    calls: list[str] = []
    result = route_request(
        method="POST",
        path="/tools/nerve_inject_sensor",
        body={"device": "emulator-5554", "sensor_type": sensor_type},
        registry=_fake_inject_registry(calls),
    )
    assert result["status"] == 400
    assert calls == [], "hostile sensor_type must be rejected before the tool runs"


def test_route_allows_legitimate_and_absent_sensor_type() -> None:
    calls: list[str] = []
    registry = _fake_inject_registry(calls)
    ok = route_request(
        method="POST",
        path="/tools/nerve_inject_sensor",
        body={"device": "emulator-5554", "sensor_type": "gyroscope"},
        registry=registry,
    )
    absent = route_request(
        method="POST",
        path="/tools/nerve_inject_sensor",
        body={"device": "emulator-5554"},
        registry=registry,
    )
    assert ok["status"] == 200
    assert absent["status"] == 200
    assert calls == ["gyroscope", "acceleration"]


def test_run_pre_dispatch_guards_covers_sensor_type() -> None:
    """The review noted `_run_pre_dispatch_guards` returned None for this
    payload — it must now reject it."""
    rejection = http_router._run_pre_dispatch_guards(
        "nerve_inject_sensor",
        {"device": "emulator-5554", "sensor_type": "acceleration\nkill"},
    )
    assert rejection is not None
    assert rejection["status"] == 400
