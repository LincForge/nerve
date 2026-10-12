"""Tests for nerve_inject_sensor MCP tool and AndroidController.inject_sensor."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from linc_nerve.core.android import AndroidController
from linc_nerve.core.ios import IOSController
from linc_nerve.mcp import server as mcp_mod


def _reset_singletons() -> None:
    """Reset module-level singletons and cache between tests."""
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


def _setup_mocks() -> MagicMock:
    """Set up standard mocks and return mock_android."""
    _reset_singletons()
    mock_android = MagicMock(spec=AndroidController)
    mock_ios = MagicMock(spec=IOSController)
    mock_ios.discover.return_value = []
    mock_android.discover.return_value = []
    mcp_mod._android = mock_android
    mcp_mod._ios = mock_ios
    mcp_mod._emulator = MagicMock()
    return mock_android


# ---------------------------------------------------------------------------
# MCP tool-level tests
# ---------------------------------------------------------------------------


class TestNerveInjectSensorMCPTool:
    """Tests for the _nerve_inject_sensor_sync MCP tool."""

    def test_rejects_physical_device(self) -> None:
        """Physical Android devices should be rejected."""
        _setup_mocks()
        result = mcp_mod._nerve_inject_sensor_sync("FAKESERIAL0001")
        assert "error" in result
        assert "not an emulator" in result["error"]

    def test_rejects_ios_simulator(self) -> None:
        """iOS simulators should be rejected."""
        _setup_mocks()
        result = mcp_mod._nerve_inject_sensor_sync("UUID-1234-5678")
        assert "error" in result
        assert "not an emulator" in result["error"]

    def test_calls_inject_sensor_on_controller(self) -> None:
        """Valid emulator serial should delegate to AndroidController.inject_sensor."""
        mock_android = _setup_mocks()
        mock_android.inject_sensor.return_value = {
            "success": True,
            "device": "emulator-5554",
            "sensor": "acceleration",
            "values": {"x": 0.0, "y": 0.0, "z": 9.81},
        }

        result = mcp_mod._nerve_inject_sensor_sync("emulator-5554")
        assert result["success"] is True
        mock_android.inject_sensor.assert_called_once_with(
            serial="emulator-5554",
            sensor_type="acceleration",
            x=0.0,
            y=0.0,
            z=9.81,
        )

    def test_custom_sensor_params(self) -> None:
        """Custom sensor type and values should be passed through."""
        mock_android = _setup_mocks()
        mock_android.inject_sensor.return_value = {
            "success": True,
            "device": "emulator-5556",
            "sensor": "gyroscope",
            "values": {"x": 1.0, "y": 2.0, "z": 3.0},
        }

        result = mcp_mod._nerve_inject_sensor_sync(
            "emulator-5556", sensor_type="gyroscope", x=1.0, y=2.0, z=3.0
        )
        assert result["success"] is True
        assert result["sensor"] == "gyroscope"

    def test_tool_registered(self) -> None:
        """nerve_inject_sensor should be registered as an MCP tool."""
        from linc_nerve.mcp._core import server

        tool_names = [t.name for t in server._tool_manager.list_tools()]
        assert "nerve_inject_sensor" in tool_names


# ---------------------------------------------------------------------------
# AndroidController core logic tests
# ---------------------------------------------------------------------------


class TestAndroidControllerInjectSensor:
    """Tests for AndroidController.inject_sensor with mocked sockets."""

    def _make_mock_socket(self, responses: list[bytes]) -> MagicMock:
        """Create a mock socket that returns responses in order."""
        mock_sock = MagicMock()
        mock_sock.recv.side_effect = responses
        return mock_sock

    def test_success_case(self) -> None:
        """Successful sensor injection via telnet console."""
        ctrl = AndroidController()
        mock_sock = self._make_mock_socket(
            [
                b"Android Console: OK\r\n",
                b"OK\r\n",
                b"OK\r\n",
            ]
        )

        with (
            patch("linc_nerve.core.android._socket_mod.socket", return_value=mock_sock),
            patch.object(AndroidController, "_read_auth_token", return_value="test_token"),
        ):
            result = ctrl.inject_sensor("emulator-5554", "acceleration", 0.0, 0.0, 9.81)

        assert result["success"] is True
        assert result["sensor"] == "acceleration"
        assert result["values"] == {"x": 0.0, "y": 0.0, "z": 9.81}

    def test_connection_refused(self) -> None:
        """Emulator not running — connection refused."""
        ctrl = AndroidController()
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = ConnectionRefusedError("Connection refused")

        with (
            patch("linc_nerve.core.android._socket_mod.socket", return_value=mock_sock),
            patch.object(AndroidController, "_read_auth_token", return_value="test_token"),
        ):
            result = ctrl.inject_sensor("emulator-5554", "acceleration", 0.0, 0.0, 9.81)

        assert "error" in result
        assert "Cannot connect" in result["error"]
        assert "5554" in result["error"]

    def test_auth_failure(self) -> None:
        """Authentication failure."""
        ctrl = AndroidController()
        mock_sock = self._make_mock_socket(
            [
                b"Android Console: OK\r\n",
                b"KO: authentication failed\r\n",
                b"",  # EOF for cleanup recv
            ]
        )

        with (
            patch("linc_nerve.core.android._socket_mod.socket", return_value=mock_sock),
            patch.object(AndroidController, "_read_auth_token", return_value="bad_token"),
        ):
            result = ctrl.inject_sensor("emulator-5554", "acceleration", 0.0, 0.0, 9.81)

        assert "error" in result
        assert "authentication failed" in result["error"].lower()

    def test_invalid_sensor_type(self) -> None:
        """An allowlisted sensor the emulator doesn't expose returns the KO as
        an error (off-list names never reach the console — security review M1)."""
        ctrl = AndroidController()
        mock_sock = self._make_mock_socket(
            [
                b"Android Console: OK\r\n",
                b"OK\r\n",
                b"KO: unknown sensor name\r\n",
            ]
        )

        with (
            patch("linc_nerve.core.android._socket_mod.socket", return_value=mock_sock),
            patch.object(AndroidController, "_read_auth_token", return_value="test_token"),
        ):
            result = ctrl.inject_sensor("emulator-5554", "hinge-angle2", 0.0, 0.0, 0.0)

        assert "error" in result
        assert "sensor set failed" in result["error"]

    def test_non_emulator_serial_raises(self) -> None:
        """Non-emulator serial raises ValueError."""
        import pytest

        with pytest.raises(ValueError, match="not an emulator"):
            AndroidController._emulator_console_port("PIXEL8PRO")

    def test_auth_token_not_found(self) -> None:
        """Missing auth token file raises FileNotFoundError."""
        import pytest

        with (
            patch("builtins.open", side_effect=FileNotFoundError()),
            pytest.raises(FileNotFoundError, match="auth token not found"),
        ):
            AndroidController._read_auth_token()

    def test_console_port_extraction(self) -> None:
        """Port extraction from emulator serial."""
        assert AndroidController._emulator_console_port("emulator-5554") == 5554
        assert AndroidController._emulator_console_port("emulator-5556") == 5556

    def test_gyroscope_sensor(self) -> None:
        """Gyroscope sensor injection."""
        ctrl = AndroidController()
        mock_sock = self._make_mock_socket(
            [
                b"Android Console: OK\r\n",
                b"OK\r\n",
                b"OK\r\n",
            ]
        )

        with (
            patch("linc_nerve.core.android._socket_mod.socket", return_value=mock_sock),
            patch.object(AndroidController, "_read_auth_token", return_value="test_token"),
        ):
            result = ctrl.inject_sensor("emulator-5554", "gyroscope", 1.5, -2.3, 0.7)

        assert result["success"] is True
        assert result["sensor"] == "gyroscope"
        assert result["values"] == {"x": 1.5, "y": -2.3, "z": 0.7}
        # Verify the sensor command was sent correctly
        calls = mock_sock.sendall.call_args_list
        sensor_cmd = [c for c in calls if b"sensor set" in c[0][0]]
        assert len(sensor_cmd) == 1
        assert b"sensor set gyroscope 1.5:-2.3:0.7" in sensor_cmd[0][0][0]
