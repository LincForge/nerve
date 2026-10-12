"""Tests for iOS controller."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.core.ios import IOSController, _classify_physical, _is_physical_udid


class TestIOSDensity:
    def test_get_density_returns_scale_factor(self) -> None:
        ctrl = IOSController()
        d = ctrl.get_density("4C215E09-3AAF-498F-B8AF-FE9F8B3B5177")
        assert "density_factor" in d
        assert d["density_factor"] >= 1.0


class TestIsPhysicalUDID:
    def test_simulator_uuid(self) -> None:
        assert _is_physical_udid("4C215E09-3AAF-498F-B8AF-FE9F8B3B5177") is False

    def test_physical_udid(self) -> None:
        assert _is_physical_udid("00008110-0000000000000001") is True

    def test_short_test_uuid(self) -> None:
        # Test UUIDs like "UUID-123" are not standard 36-char UUIDs
        assert _is_physical_udid("UUID-123") is True


class TestIOSDiscovery:
    def test_discover_booted_simulator(self) -> None:
        ctrl = IOSController()
        simctl_output = json.dumps(
            {
                "devices": {
                    "com.apple.CoreSimulator.SimRuntime.iOS-17-5": [
                        {
                            "name": "iPhone 15 Pro",
                            "udid": "AAAA-BBBB-CCCC",
                            "state": "Booted",
                            "isAvailable": True,
                        },
                        {
                            "name": "iPhone 15",
                            "udid": "DDDD-EEEE-FFFF",
                            "state": "Shutdown",
                            "isAvailable": True,
                        },
                    ]
                }
            }
        )

        with (
            patch.object(ctrl, "_run") as mock_run,
            patch.object(ctrl, "_discover_physical", return_value=[]),
        ):
            mock_run.return_value = MagicMock(stdout=simctl_output)
            devices = ctrl.discover()

        assert len(devices) == 2
        booted = [d for d in devices if d.state == "online"]
        assert len(booted) == 1
        assert booted[0].serial == "AAAA-BBBB-CCCC"
        assert booted[0].model == "iPhone 15 Pro"
        assert booted[0].platform == Platform.IOS
        assert booted[0].device_type == DeviceType.PHONE

    def test_discover_no_simulators(self) -> None:
        ctrl = IOSController()
        with (
            patch.object(ctrl, "_run") as mock_run,
            patch.object(ctrl, "_discover_physical", return_value=[]),
        ):
            mock_run.return_value = MagicMock(stdout='{"devices": {}}')
            devices = ctrl.discover()

        assert devices == []

    def test_discover_simctl_unavailable(self) -> None:
        ctrl = IOSController()
        with (
            patch.object(ctrl, "_run") as mock_run,
            patch.object(ctrl, "_discover_physical", return_value=[]),
        ):
            mock_run.side_effect = FileNotFoundError("xcrun not found")
            devices = ctrl.discover()

        assert devices == []

    def test_discover_extracts_os_version(self) -> None:
        ctrl = IOSController()
        simctl_output = json.dumps(
            {
                "devices": {
                    "com.apple.CoreSimulator.SimRuntime.iOS-18-1": [
                        {
                            "name": "iPhone 16",
                            "udid": "UUID-123",
                            "state": "Booted",
                        },
                    ]
                }
            }
        )

        with (
            patch.object(ctrl, "_run") as mock_run,
            patch.object(ctrl, "_discover_physical", return_value=[]),
        ):
            mock_run.return_value = MagicMock(stdout=simctl_output)
            devices = ctrl.discover()

        assert devices[0].os_version == "18.1"

    def test_discover_physical_devices(self) -> None:
        ctrl = IOSController()
        xctrace_output = (
            "== Devices ==\n"
            "Display's MacBook Pro (C2DBBDDE-E05C-5B33-8E42-EF3C9DC11FF0)\n"
            "Test iPhone (18.7.2) (00008110-0000000000000001)\n"
            "Test Apple Watch (26.2.1) (00008310-0000000000000002)\n"
            "\n"
            "== Devices Offline ==\n"
            "Test iPhone Offline (26.3) (00008140-0000000000000003)\n"
            "\n"
            "== Simulators ==\n"
            "iPhone 16 Pro Simulator (18.1) (4C215E09-3AAF-498F-B8AF-FE9F8B3B5177)\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=xctrace_output, returncode=0)
            devices = ctrl._discover_physical_xctrace()

        assert len(devices) == 3
        # Online devices
        iphone = [d for d in devices if "iPhone" in d.model and d.state == "online"]
        assert len(iphone) == 1
        assert iphone[0].serial == "00008110-0000000000000001"
        assert iphone[0].os_version == "18.7.2"
        assert iphone[0].device_type == DeviceType.PHONE

        watch = [d for d in devices if "Watch" in d.model]
        assert len(watch) == 1
        assert watch[0].device_type == DeviceType.WATCH
        assert watch[0].state == "online"

        # Offline device
        offline = [d for d in devices if d.state == "offline"]
        assert len(offline) == 1
        assert offline[0].serial == "00008140-0000000000000003"

    def test_discover_physical_skips_mac(self) -> None:
        ctrl = IOSController()
        xctrace_output = (
            "== Devices ==\n"
            "Display's MacBook Pro (C2DBBDDE-E05C-5B33-8E42-EF3C9DC11FF0)\n"
            "\n"
            "== Simulators ==\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=xctrace_output, returncode=0)
            devices = ctrl._discover_physical_xctrace()

        assert len(devices) == 0

    def test_discover_physical_xctrace_unavailable(self) -> None:
        ctrl = IOSController()
        with patch("subprocess.run", side_effect=FileNotFoundError("xctrace")):
            devices = ctrl._discover_physical_xctrace()
        assert devices == []


SIM_UUID = "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"
PHYS_UDID = "00008110-0000000000000001"


class TestIOSScreenshot:
    def test_screenshot_simulator(self) -> None:
        ctrl = IOSController()
        fake_png = b"\x89PNG\r\n\x1a\nfakedata"

        # Force the simctl fallback path (mjpeg_port=None disables the fast path),
        # deterministically and without touching a real device/WDA.
        session = MagicMock(mjpeg_port=None)

        def fake_run_binary(*args, timeout=15):
            # simctl writes to a real file path (not stdout '-', rejected on iOS 26).
            with open(args[-1], "wb") as f:
                f.write(fake_png)
            return MagicMock(stdout=b"")

        with (
            patch.object(ctrl, "_is_physical", return_value=False),
            patch.object(ctrl, "_get_wda", return_value=session),
            patch.object(ctrl, "_run_binary", side_effect=fake_run_binary) as mock_run,
        ):
            data = ctrl.screenshot(SIM_UUID)

        assert data == fake_png
        # io/screenshot/--type=png + a real path (not "-")
        called_args = mock_run.call_args.args
        assert called_args[:4] == ("io", SIM_UUID, "screenshot", "--type=png")
        assert called_args[4] != "-"

    def test_screenshot_physical_uses_wda(self) -> None:
        ctrl = IOSController()
        import base64

        fake_png = b"\x89PNG\r\n\x1a\nfakedata"
        b64_png = base64.b64encode(fake_png).decode()

        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"value": b64_png}
        mock_response.raise_for_status = MagicMock()
        mock_session._request.return_value = mock_response

        phone_dev = MagicMock(serial=PHYS_UDID, device_type=DeviceType.PHONE)
        with (
            patch.object(ctrl, "discover", return_value=[phone_dev]),
            patch.object(ctrl._wda, "get_or_start", return_value=mock_session),
        ):
            data = ctrl.screenshot(PHYS_UDID)

        assert data == fake_png
        mock_session._request.assert_called_once_with("GET", "/screenshot")

    def test_screenshot_physical_watch_raises_before_wda(self) -> None:
        """A physical Apple Watch serial raises before touching WDA at all —
        no silent substitution with the paired iPhone's session
        (reactor d94ec895)."""
        ctrl = IOSController()
        watch_udid = "00008310-001A2D3E1401A01E"
        watch_dev = MagicMock(serial=watch_udid, device_type=DeviceType.WATCH)

        with (
            patch.object(ctrl, "_is_physical", return_value=True),
            patch.object(ctrl, "discover", return_value=[watch_dev]),
            patch.object(ctrl, "_get_wda") as mock_get_wda,
            pytest.raises(NotImplementedError) as exc_info,
        ):
            ctrl.screenshot(watch_udid)

        assert "watchOS" in str(exc_info.value)
        assert watch_udid in str(exc_info.value)
        mock_get_wda.assert_not_called()

    def test_screenshot_physical_iphone_reaches_wda(self) -> None:
        """A physical iPhone (device_type PHONE) still reaches _get_wda in
        _screenshot_physical — pins the non-watch path after the
        _require_wda guard was added (reactor d94ec895)."""
        ctrl = IOSController()
        import base64

        fake_png = b"\x89PNG\r\n\x1a\nfakedata"
        b64_png = base64.b64encode(fake_png).decode()

        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"value": b64_png}
        mock_response.raise_for_status = MagicMock()
        mock_session._request.return_value = mock_response

        phone_dev = MagicMock(serial=PHYS_UDID, device_type=DeviceType.PHONE)
        with (
            patch.object(ctrl, "_is_physical", return_value=True),
            patch.object(ctrl, "discover", return_value=[phone_dev]),
            patch.object(ctrl, "_get_wda", return_value=mock_session) as mock_get_wda,
        ):
            data = ctrl.screenshot(PHYS_UDID)

        assert data == fake_png
        mock_get_wda.assert_called_once_with(PHYS_UDID)

    def test_screenshot_simulator_watch_skips_wda(self) -> None:
        """When device discovery reports DeviceType.WATCH, screenshot never calls
        _get_wda and does call _run_binary('io', watch_udid, 'screenshot', '--type=png', <path>).
        """
        ctrl = IOSController()
        watch_udid = "35FE5643-28E2-452F-B430-BB5D16ED862A"
        fake_png = b"\x89PNG\r\n\x1a\nwatchdata"

        watch_dev = MagicMock(serial=watch_udid, device_type=DeviceType.WATCH)

        def fake_run_binary(*args, timeout=15):
            with open(args[-1], "wb") as f:
                f.write(fake_png)
            return MagicMock(stdout=b"")

        with (
            patch.object(ctrl, "_is_physical", return_value=False),
            patch.object(ctrl, "discover", return_value=[watch_dev]),
            patch.object(ctrl, "_get_wda") as mock_get_wda,
            patch.object(ctrl, "_run_binary", side_effect=fake_run_binary) as mock_run,
        ):
            data = ctrl.screenshot(watch_udid)

        assert data == fake_png
        mock_get_wda.assert_not_called()
        called_args = mock_run.call_args.args
        assert called_args[:4] == ("io", watch_udid, "screenshot", "--type=png")
        assert called_args[4] != "-"

    def test_screenshot_watch_ignores_wda_mjpeg_session(self) -> None:
        """For a watch serial, the returned screenshot comes from simctl fallback.
        If _get_wda would have returned a session with an mjpeg_port, its frame is NOT used.
        """
        ctrl = IOSController()
        watch_udid = "35FE5643-28E2-452F-B430-BB5D16ED862A"
        simctl_png = b"\x89PNG\r\n\x1a\nwatchsimctl"
        wda_png = b"\x89PNG\r\n\x1a\niphonewdaframe"

        watch_dev = MagicMock(serial=watch_udid, device_type=DeviceType.WATCH)
        wda_session = MagicMock(mjpeg_port=8101)

        def fake_run_binary(*args, timeout=15):
            with open(args[-1], "wb") as f:
                f.write(simctl_png)
            return MagicMock(stdout=b"")

        with (
            patch.object(ctrl, "_is_physical", return_value=False),
            patch.object(ctrl, "discover", return_value=[watch_dev]),
            patch.object(ctrl, "_get_wda", return_value=wda_session) as mock_get_wda,
            patch.object(ctrl._mjpeg, "is_open", return_value=False),
            patch.object(ctrl._mjpeg, "screenshot", return_value=wda_png),
            patch.object(ctrl, "_run_binary", side_effect=fake_run_binary),
        ):
            data = ctrl.screenshot(watch_udid, full_resolution=True)

        assert data == simctl_png
        mock_get_wda.assert_not_called()

    def test_screenshot_simulator_iphone_uses_mjpeg_fast_path(self) -> None:
        """An iPhone-simulator serial still takes the MJPEG fast path
        (_get_wda is consulted, and the frame is returned when mjpeg_port is set).
        """
        ctrl = IOSController()
        iphone_udid = SIM_UUID
        mjpeg_png = b"\x89PNG\r\n\x1a\niphonemjpeg"

        iphone_dev = MagicMock(serial=iphone_udid, device_type=DeviceType.PHONE)
        wda_session = MagicMock(mjpeg_port=8101)

        with (
            patch.object(ctrl, "_is_physical", return_value=False),
            patch.object(ctrl, "discover", return_value=[iphone_dev]),
            patch.object(ctrl, "_get_wda", return_value=wda_session) as mock_get_wda,
            patch.object(ctrl._mjpeg, "is_open", return_value=False),
            patch.object(ctrl._mjpeg, "screenshot", return_value=mjpeg_png),
            patch.object(ctrl, "_run_binary") as mock_run_binary,
        ):
            data = ctrl.screenshot(iphone_udid, full_resolution=True)

        assert data == mjpeg_png
        mock_get_wda.assert_called_once_with(iphone_udid)
        mock_run_binary.assert_not_called()

    def test_screenshot_iphone_caches_device_type_lookup(self) -> None:
        """Two consecutive iPhone screenshots trigger at most one device-type discovery call
        (the type lookup is cached), so the hot path gains no per-call xcrun simctl list.
        """
        ctrl = IOSController()
        iphone_udid = SIM_UUID
        mjpeg_png = b"\x89PNG\r\n\x1a\niphonemjpeg"

        iphone_dev = MagicMock(serial=iphone_udid, device_type=DeviceType.PHONE)
        wda_session = MagicMock(mjpeg_port=8101)

        with (
            patch.object(ctrl, "_is_physical", return_value=False),
            patch.object(ctrl, "discover", return_value=[iphone_dev]) as mock_discover,
            patch.object(ctrl, "_get_wda", return_value=wda_session),
            patch.object(ctrl._mjpeg, "is_open", return_value=False),
            patch.object(ctrl._mjpeg, "screenshot", return_value=mjpeg_png),
        ):
            data1 = ctrl.screenshot(iphone_udid, full_resolution=True)
            data2 = ctrl.screenshot(iphone_udid, full_resolution=True)

        assert data1 == mjpeg_png
        assert data2 == mjpeg_png
        assert mock_discover.call_count == 1

    def test_screenshot_watch_retries_discovery_after_initial_miss(self) -> None:
        """A discovery miss is not cached permanently: if discover() initially returns []
        (transient boot-up or simctl glitch) and later returns the watch device,
        the second screenshot(watch_udid) resolves the type, skips _get_wda, and routes to simctl.
        """
        ctrl = IOSController()
        watch_udid = "35FE5643-28E2-452F-B430-BB5D16ED862A"
        simctl_png = b"\x89PNG\r\n\x1a\nwatchsimctl"
        watch_dev = MagicMock(serial=watch_udid, device_type=DeviceType.WATCH)

        def fake_run_binary(*args, timeout=15):
            with open(args[-1], "wb") as f:
                f.write(simctl_png)
            return MagicMock(returncode=0, stdout=b"")

        # Call 1: discover returns empty list; Call 2: discover returns watch device
        with (
            patch.object(ctrl, "_is_physical", return_value=False),
            patch.object(ctrl, "discover", side_effect=[[], [watch_dev]]) as mock_discover,
            patch.object(ctrl, "_get_wda") as mock_get_wda,
            patch.object(ctrl, "_run_binary", side_effect=fake_run_binary),
        ):
            mock_get_wda.return_value = MagicMock(mjpeg_port=None)
            data1 = ctrl.screenshot(watch_udid, full_resolution=True)
            assert data1 == simctl_png
            assert mock_get_wda.call_count == 1

            mock_get_wda.reset_mock()
            data2 = ctrl.screenshot(watch_udid, full_resolution=True)
            assert data2 == simctl_png
            mock_get_wda.assert_not_called()
            assert mock_discover.call_count == 2

    def test_screenshot_simctl_failed_capture_raises(self) -> None:
        """When simctl screenshot fails (subprocess error, non-zero exit, or empty file),
        screenshot() propagates an error rather than returning an empty payload.
        """
        ctrl = IOSController()
        watch_udid = "35FE5643-28E2-452F-B430-BB5D16ED862A"
        watch_dev = MagicMock(serial=watch_udid, device_type=DeviceType.WATCH)

        # Case 1: _run_binary raises CalledProcessError
        with (
            patch.object(ctrl, "_is_physical", return_value=False),
            patch.object(ctrl, "discover", return_value=[watch_dev]),
            patch.object(
                ctrl,
                "_run_binary",
                side_effect=subprocess.CalledProcessError(1, ["xcrun", "simctl"]),
            ),
            pytest.raises(subprocess.CalledProcessError),
        ):
            ctrl.screenshot(watch_udid)

        # Case 2: _run_binary returns non-zero returncode
        with (
            patch.object(ctrl, "_is_physical", return_value=False),
            patch.object(ctrl, "discover", return_value=[watch_dev]),
            patch.object(ctrl, "_run_binary", return_value=MagicMock(returncode=1)),
            pytest.raises(RuntimeError, match="simctl screenshot failed"),
        ):
            ctrl.screenshot(watch_udid)

        # Case 3: _run_binary writes an empty file (zero bytes)
        def fake_empty_run_binary(*args, timeout=15):
            open(args[-1], "wb").close()
            return MagicMock(returncode=0, stdout=b"")

        with (
            patch.object(ctrl, "_is_physical", return_value=False),
            patch.object(ctrl, "discover", return_value=[watch_dev]),
            patch.object(ctrl, "_run_binary", side_effect=fake_empty_run_binary),
            pytest.raises(RuntimeError, match="empty output"),
        ):
            ctrl.screenshot(watch_udid)


class TestIOSInput:
    def test_tap_delegates_to_wda(self) -> None:
        ctrl = IOSController()
        mock_session = MagicMock()
        with (
            patch.object(ctrl, "_require_wda"),
            patch.object(ctrl._wda, "get_or_start", return_value=mock_session),
        ):
            ctrl.tap("UUID", 100, 200)
        mock_session.tap.assert_called_once_with(100, 200)

    def test_swipe_delegates_to_wda(self) -> None:
        ctrl = IOSController()
        mock_session = MagicMock()
        with (
            patch.object(ctrl, "_require_wda"),
            patch.object(ctrl._wda, "get_or_start", return_value=mock_session),
        ):
            ctrl.swipe("UUID", 0, 0, 100, 100, 300)
        mock_session.swipe.assert_called_once_with(0, 0, 100, 100, 300)

    def test_tap_watch_not_supported(self) -> None:
        ctrl = IOSController()
        # Mock discover to return a watch device
        from linc_nerve.core.device import DeviceType, Platform

        watch = NerveDevice(
            serial="WATCH-UUID",
            platform=Platform.IOS,
            device_type=DeviceType.WATCH,
            model="Apple Watch Series 10",
        )
        with (
            patch.object(ctrl, "discover", return_value=[watch]),
            pytest.raises(NotImplementedError, match="watchOS"),
        ):
            ctrl.tap("WATCH-UUID", 100, 200)

    def test_elements_delegates_to_wda(self) -> None:
        ctrl = IOSController()
        mock_session = MagicMock()
        mock_session.source.return_value = {
            "value": {
                "type": "Application",
                "label": "iosApp",
                "children": [
                    {
                        "type": "Button",
                        "rawIdentifier": "btn",
                        "label": "OK",
                        "frame": "{{0, 0}, {100, 50}}",
                        "isEnabled": "1",
                        "children": [],
                    },
                ],
            },
        }
        with (
            patch.object(ctrl, "_require_wda"),
            patch.object(ctrl._wda, "get_or_start", return_value=mock_session),
        ):
            result = ctrl.elements("UUID")
        assert len(result) == 1
        assert result[0]["text"] == "OK"

    def test_type_text_simulator(self) -> None:
        ctrl = IOSController()
        with patch.object(ctrl, "_run") as mock_run:
            ctrl.type_text(SIM_UUID, "hello")
        mock_run.assert_called_once_with("io", SIM_UUID, "input", "text", "hello")

    def test_type_text_physical_uses_wda(self) -> None:
        ctrl = IOSController()
        mock_session = MagicMock()
        phone_dev = MagicMock(serial=PHYS_UDID, device_type=DeviceType.PHONE)
        with (
            patch.object(ctrl, "discover", return_value=[phone_dev]),
            patch.object(ctrl._wda, "get_or_start", return_value=mock_session),
        ):
            ctrl.type_text(PHYS_UDID, "hello")
        mock_session._request.assert_called_once_with(
            "POST",
            "/wda/keys",
            json={"value": ["h", "e", "l", "l", "o"]},
        )

    def test_type_text_physical_watch_raises_before_wda(self) -> None:
        ctrl = IOSController()
        watch_udid = "00008310-001A2D3E1401A01E"
        watch_dev = MagicMock(serial=watch_udid, device_type=DeviceType.WATCH)

        with (
            patch.object(ctrl, "_is_physical", return_value=True),
            patch.object(ctrl, "discover", return_value=[watch_dev]),
            patch.object(ctrl, "_get_wda") as mock_get_wda,
            pytest.raises(NotImplementedError) as exc_info,
        ):
            ctrl.type_text(watch_udid, "x")

        assert "watchOS" in str(exc_info.value)
        assert watch_udid in str(exc_info.value)
        mock_get_wda.assert_not_called()

    def test_press_key_physical_watch_raises_before_wda(self) -> None:
        ctrl = IOSController()
        watch_udid = "00008310-001A2D3E1401A01E"
        watch_dev = MagicMock(serial=watch_udid, device_type=DeviceType.WATCH)

        with (
            patch.object(ctrl, "_is_physical", return_value=True),
            patch.object(ctrl, "discover", return_value=[watch_dev]),
            patch.object(ctrl, "_get_wda") as mock_get_wda,
            pytest.raises(NotImplementedError) as exc_info,
        ):
            ctrl.press_key(watch_udid, "home")

        assert "watchOS" in str(exc_info.value)
        assert watch_udid in str(exc_info.value)
        mock_get_wda.assert_not_called()

    def test_press_key_physical_non_home_watch_raises_before_wda(self) -> None:
        """Non-HOME keys must also raise before WDA is touched — not just
        silently no-op like the pre-fix "unsupported key" debug path."""
        ctrl = IOSController()
        watch_udid = "00008310-001A2D3E1401A01E"
        watch_dev = MagicMock(serial=watch_udid, device_type=DeviceType.WATCH)

        with (
            patch.object(ctrl, "_is_physical", return_value=True),
            patch.object(ctrl, "discover", return_value=[watch_dev]),
            patch.object(ctrl, "_get_wda") as mock_get_wda,
            pytest.raises(NotImplementedError),
        ):
            ctrl.press_key(watch_udid, "volume_up")

        mock_get_wda.assert_not_called()


class TestIOSAppLifecycle:
    def test_install_simulator(self) -> None:
        ctrl = IOSController()
        with patch.object(ctrl, "_run") as mock_run:
            ctrl.install(SIM_UUID, "/path/to/App.app")
        mock_run.assert_called_once_with("install", SIM_UUID, "/path/to/App.app", timeout=120)

    def test_install_physical(self) -> None:
        ctrl = IOSController()
        with patch.object(ctrl, "_run_devicectl") as mock_run:
            ctrl.install(PHYS_UDID, "/path/to/App.app")
        mock_run.assert_called_once_with(
            "device",
            "install",
            "app",
            "--device",
            PHYS_UDID,
            "/path/to/App.app",
            timeout=120,
        )

    def test_install_physical_retries_once_on_transient_3002_error(self) -> None:
        """A transient devicectl 3002/bookmark resolution error is retried once."""
        ctrl = IOSController()
        transient_error = subprocess.CalledProcessError(
            returncode=70,
            cmd=["xcrun", "devicectl", "device", "install", "app"],
            stderr=(
                "ERROR: A device error occurred: The specified device was not "
                "found because it could not be located during the last "
                "connection. (com.apple.dt.CoreDeviceError 3002)"
            ),
        )
        with patch.object(
            ctrl, "_run_devicectl", side_effect=[transient_error, MagicMock()]
        ) as mock_run:
            ctrl.install(PHYS_UDID, "/path/to/App.app")
        assert mock_run.call_count == 2
        mock_run.assert_called_with(
            "device",
            "install",
            "app",
            "--device",
            PHYS_UDID,
            "/path/to/App.app",
            timeout=120,
        )

    def test_install_physical_bookmark_error_message_also_retries(self) -> None:
        """The transient error is also recognized by a 'bookmark' message (no code)."""
        ctrl = IOSController()
        transient_error = subprocess.CalledProcessError(
            returncode=70,
            cmd=["xcrun", "devicectl", "device", "install", "app"],
            stderr="ERROR: could not resolve device bookmark",
        )
        with patch.object(
            ctrl, "_run_devicectl", side_effect=[transient_error, MagicMock()]
        ) as mock_run:
            ctrl.install(PHYS_UDID, "/path/to/App.app")
        assert mock_run.call_count == 2

    def test_install_physical_fails_after_second_transient_error(self) -> None:
        """Only one retry is attempted — a second transient failure propagates."""
        ctrl = IOSController()
        transient_error = subprocess.CalledProcessError(
            returncode=70,
            cmd=["xcrun", "devicectl", "device", "install", "app"],
            stderr="ERROR: 3002 device bookmark could not be resolved",
        )
        with (
            patch.object(
                ctrl, "_run_devicectl", side_effect=[transient_error, transient_error]
            ) as mock_run,
            pytest.raises(subprocess.CalledProcessError),
        ):
            ctrl.install(PHYS_UDID, "/path/to/App.app")
        assert mock_run.call_count == 2

    def test_install_physical_non_transient_error_not_retried(self) -> None:
        """A non-transient devicectl failure is not retried."""
        ctrl = IOSController()
        other_error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["xcrun", "devicectl", "device", "install", "app"],
            stderr="ERROR: code signature invalid",
        )
        with (
            patch.object(ctrl, "_run_devicectl", side_effect=[other_error]) as mock_run,
            pytest.raises(subprocess.CalledProcessError),
        ):
            ctrl.install(PHYS_UDID, "/path/to/App.app")
        assert mock_run.call_count == 1

    def test_install_simulator_error_not_retried(self) -> None:
        """Simulator install failures don't go through the physical retry path."""
        ctrl = IOSController()
        sim_error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["xcrun", "simctl", "install"],
            stderr="ERROR: 3002 bookmark",
        )
        with (
            patch.object(ctrl, "_run", side_effect=[sim_error]) as mock_run,
            pytest.raises(subprocess.CalledProcessError),
        ):
            ctrl.install(SIM_UUID, "/path/to/App.app")
        assert mock_run.call_count == 1

    def test_launch_simulator(self) -> None:
        ctrl = IOSController()
        with patch.object(ctrl, "_run") as mock_run:
            ctrl.launch(SIM_UUID, "com.app.bundle")
        mock_run.assert_called_once_with("launch", SIM_UUID, "com.app.bundle")

    def test_launch_physical(self) -> None:
        ctrl = IOSController()
        with patch.object(ctrl, "_run_devicectl") as mock_run:
            ctrl.launch(PHYS_UDID, "com.app.bundle")
        mock_run.assert_called_once_with(
            "device",
            "process",
            "launch",
            "--device",
            PHYS_UDID,
            "com.app.bundle",
        )

    def test_launch_with_uri_simulator(self) -> None:
        ctrl = IOSController()
        with patch.object(ctrl, "_run") as mock_run:
            ctrl.launch(SIM_UUID, "com.app.bundle", uri="example://home")
        mock_run.assert_called_once_with("openurl", SIM_UUID, "example://home")

    def test_launch_with_uri_physical(self) -> None:
        ctrl = IOSController()
        mock_session = MagicMock()
        with patch.object(ctrl, "_get_wda", return_value=mock_session):
            ctrl.launch(PHYS_UDID, "com.app.bundle", uri="example://home")
        mock_session._request.assert_called_once_with(
            "POST", "/url", json={"url": "example://home"}
        )

    def test_launch_simulator_with_extras(self) -> None:
        """Simulator extras map to simctl launch args (NSUserDefaults argument domain).

        bool -> -KEY YES/NO, so a consent-gated app reads SKIP_CONSENT via
        UserDefaults without a manual priming step (iOS analog of AC1/AC3).
        """
        ctrl = IOSController()
        with patch.object(ctrl, "_run") as mock_run:
            ctrl.launch(SIM_UUID, "com.app.bundle", extras={"SKIP_CONSENT": True})
        mock_run.assert_called_once_with(
            "launch", SIM_UUID, "com.app.bundle", "-SKIP_CONSENT", "YES"
        )

    def test_launch_simulator_with_extras_mixed_types(self) -> None:
        ctrl = IOSController()
        with patch.object(ctrl, "_run") as mock_run:
            ctrl.launch(SIM_UUID, "com.app.bundle", extras={"FLAG": False, "SEED": 7, "MODE": "x"})
        mock_run.assert_called_once_with(
            "launch", SIM_UUID, "com.app.bundle", "-FLAG", "NO", "-SEED", "7", "-MODE", "x"
        )

    def test_launch_physical_with_extras_raises(self) -> None:
        """Physical-device extras are unsupported (devicectl arg passing unreliable)."""
        ctrl = IOSController()
        with pytest.raises(NotImplementedError, match="physical"):
            ctrl.launch(PHYS_UDID, "com.app.bundle", extras={"SKIP_CONSENT": True})

    def test_launch_uri_with_extras_raises(self) -> None:
        """Extras can't ride the openurl/WDA deep-link path."""
        ctrl = IOSController()
        with pytest.raises(NotImplementedError, match="uri"):
            ctrl.launch(SIM_UUID, "com.app.bundle", uri="example://home", extras={"X": True})

    def test_launch_invalid_extras_type_raises(self) -> None:
        ctrl = IOSController()
        with pytest.raises(ValueError, match="extras"):
            ctrl.launch(SIM_UUID, "com.app.bundle", extras={"BAD": 3.14})

    def test_kill_simulator(self) -> None:
        ctrl = IOSController()
        with patch.object(ctrl, "_run") as mock_run:
            ctrl.kill(SIM_UUID, "com.app.bundle")
        mock_run.assert_called_once_with("terminate", SIM_UUID, "com.app.bundle", check=False)

    def test_kill_physical(self) -> None:
        ctrl = IOSController()
        with patch.object(ctrl, "_run_devicectl") as mock_run:
            ctrl.kill(PHYS_UDID, "com.app.bundle")
        mock_run.assert_called_once_with(
            "device",
            "process",
            "terminate",
            "--device",
            PHYS_UDID,
            "com.app.bundle",
            check=False,
        )

    def test_uninstall_simulator(self) -> None:
        ctrl = IOSController()
        with patch.object(ctrl, "_run") as mock_run:
            ctrl.uninstall(SIM_UUID, "com.app.bundle")
        mock_run.assert_called_once_with("uninstall", SIM_UUID, "com.app.bundle")

    def test_uninstall_physical(self) -> None:
        ctrl = IOSController()
        with patch.object(ctrl, "_run_devicectl") as mock_run:
            ctrl.uninstall(PHYS_UDID, "com.app.bundle")
        mock_run.assert_called_once_with(
            "device",
            "install",
            "uninstall",
            "--device",
            PHYS_UDID,
            "com.app.bundle",
        )


class TestIOSClearAppData:
    """ios-sim cold_start: uninstall-based clear_app_data with the
    needs_reinstall contract (047 Phase B2)."""

    def test_clear_app_data_simulator_uninstalls_and_returns_true(self) -> None:
        """Sim clear_app_data = simctl uninstall; True = needs_reinstall."""
        ctrl = IOSController()
        with patch.object(ctrl, "_run") as mock_run:
            result = ctrl.clear_app_data(SIM_UUID, "com.app.bundle")
        mock_run.assert_called_once_with("uninstall", SIM_UUID, "com.app.bundle")
        assert result is True

    def test_clear_app_data_not_installed_is_idempotent(self) -> None:
        """First-ever cold_start: app absent → still True, no raise.

        The swallowed failure must leave a structured debug log so marker
        drift (new simctl wording) stays observable.
        """
        ctrl = IOSController()
        exc = subprocess.CalledProcessError(
            1,
            ["xcrun", "simctl", "uninstall"],
            output="",
            stderr=("An error was encountered processing the command: No such file or directory"),
        )
        with (
            patch.object(ctrl, "_run", side_effect=exc) as mock_run,
            patch("linc_nerve.core.ios.logger") as mock_logger,
        ):
            result = ctrl.clear_app_data(SIM_UUID, "com.app.bundle")
        mock_run.assert_called_once_with("uninstall", SIM_UUID, "com.app.bundle")
        assert result is True
        mock_logger.debug.assert_called_once_with(
            "clear_app_data_not_installed",
            serial=SIM_UUID[:12],
            bundle_id="com.app.bundle",
            stderr=exc.stderr[:200],
        )

    def test_clear_app_data_not_installed_variant_case_insensitive(self) -> None:
        """The 'not installed' wording variant matches case-insensitively."""
        ctrl = IOSController()
        exc = subprocess.CalledProcessError(
            1,
            ["xcrun", "simctl", "uninstall"],
            output="com.app.bundle is NOT INSTALLED on this device",
            stderr="",
        )
        with patch.object(ctrl, "_run", side_effect=exc):
            assert ctrl.clear_app_data(SIM_UUID, "com.app.bundle") is True

    def test_clear_app_data_other_error_reraises(self) -> None:
        """Non 'not-installed' failures propagate untouched."""
        ctrl = IOSController()
        exc = subprocess.CalledProcessError(
            1,
            ["xcrun", "simctl", "uninstall"],
            output="",
            stderr="Invalid device state: Shutdown",
        )
        with (
            patch.object(ctrl, "_run", side_effect=exc),
            pytest.raises(subprocess.CalledProcessError),
        ):
            ctrl.clear_app_data(SIM_UUID, "com.app.bundle")

    def test_clear_app_data_physical_raises(self) -> None:
        """Physical iOS has no clean wipe primitive — explicit guard stays."""
        ctrl = IOSController()
        with pytest.raises(NotImplementedError, match="physical"):
            ctrl.clear_app_data(PHYS_UDID, "com.app.bundle")


class TestIOSGetLogs:
    def test_get_logs_simulator_no_filter(self) -> None:
        ctrl = IOSController()
        mock_result = MagicMock()
        mock_result.stdout = "line1\nline2\nline3"
        mock_result.returncode = 0
        with patch.object(ctrl, "_run", return_value=mock_result) as mock_run:
            result = ctrl.get_logs(SIM_UUID, lines=50)
        mock_run.assert_called_once_with(
            "spawn",
            SIM_UUID,
            "log",
            "show",
            "--last",
            "5m",
            "--style",
            "syslog",
            check=False,
            timeout=30,
        )
        assert result == "line1\nline2\nline3"

    def test_get_logs_simulator_with_process_filter(self) -> None:
        ctrl = IOSController()
        mock_result = MagicMock()
        mock_result.stdout = "filtered line"
        mock_result.returncode = 0
        with patch.object(ctrl, "_run", return_value=mock_result) as mock_run:
            ctrl.get_logs(SIM_UUID, lines=50, filter="SniperPulse")
        mock_run.assert_called_once_with(
            "spawn",
            SIM_UUID,
            "log",
            "show",
            "--last",
            "5m",
            "--style",
            "syslog",
            "--predicate",
            'process == "SniperPulse"',
            check=False,
            timeout=30,
        )

    def test_get_logs_physical(self) -> None:
        ctrl = IOSController()
        mock_result = MagicMock()
        mock_result.stdout = "device log line"
        mock_result.returncode = 0
        with patch.object(ctrl, "_run_log", return_value=mock_result) as mock_run:
            result = ctrl.get_logs(PHYS_UDID, lines=50)
        mock_run.assert_called_once_with(
            "show",
            "--device",
            PHYS_UDID,
            "--last",
            "5m",
            "--style",
            "syslog",
            check=False,
            timeout=30,
        )
        assert result == "device log line"

    def test_get_log_stream_cmd_simulator(self) -> None:
        ctrl = IOSController()
        cmd = ctrl.get_log_stream_cmd(SIM_UUID)
        assert cmd == ["xcrun", "simctl", "spawn", SIM_UUID, "log", "stream", "--style", "syslog"]

        with patch.object(ctrl, "_resolve_bundle_id", return_value="SniperPulse"):
            cmd = ctrl.get_log_stream_cmd(SIM_UUID, filter_str="com.app.bundle")
            assert cmd == [
                "xcrun",
                "simctl",
                "spawn",
                SIM_UUID,
                "log",
                "stream",
                "--style",
                "syslog",
                "--predicate",
                'process == "SniperPulse"',
            ]

    def test_get_log_stream_cmd_physical(self) -> None:
        ctrl = IOSController()
        cmd = ctrl.get_log_stream_cmd(PHYS_UDID)
        assert cmd == ["log", "stream", "--device", PHYS_UDID, "--style", "syslog"]

        with patch.object(ctrl, "_resolve_bundle_id", return_value="SniperPulse"):
            cmd = ctrl.get_log_stream_cmd(PHYS_UDID, filter_str="com.app.bundle")
            assert cmd == [
                "log",
                "stream",
                "--device",
                PHYS_UDID,
                "--style",
                "syslog",
                "--predicate",
                'process == "SniperPulse"',
            ]

    def test_get_log_stream_cmd_rejects_leading_dash_filter(self) -> None:
        """linc-nerve#128/129 round 1 finding 3: a flag-shaped filter must
        be rejected before it ever reaches this method's subprocess argv."""
        ctrl = IOSController()
        with pytest.raises(ValueError, match="Invalid filter"):
            ctrl.get_log_stream_cmd(SIM_UUID, filter_str="-c")

    def test_resolve_bundle_id_simulator(self) -> None:
        ctrl = IOSController()
        listapps_output = (
            '    CFBundleIdentifier = "com.lincmobile.sniperpulse";\n'
            '    CFBundleExecutable = "SniperPulse";\n'
        )
        mock_listapps = MagicMock()
        mock_listapps.stdout = listapps_output
        mock_listapps.returncode = 0

        mock_log = MagicMock()
        mock_log.stdout = "log line"
        mock_log.returncode = 0

        def side_effect(*args: Any, **kwargs: Any) -> Any:
            if args[0] == "listapps":
                return mock_listapps
            return mock_log

        with patch.object(ctrl, "_run", side_effect=side_effect) as mock_run:
            ctrl.get_logs(SIM_UUID, filter="com.lincmobile.sniperpulse")
        # The log call should use the resolved process name
        log_call = next(c for c in mock_run.call_args_list if c[0][0] == "spawn")
        assert 'process == "SniperPulse"' in log_call[0]

    def test_resolve_bundle_id_not_found(self) -> None:
        ctrl = IOSController()
        mock_result = MagicMock()
        mock_result.stdout = (
            '    CFBundleIdentifier = "com.other.app";\n    CFBundleExecutable = "Other";\n'
        )
        mock_result.returncode = 0
        with (
            patch.object(ctrl, "_run", return_value=mock_result),
            pytest.raises(ValueError, match="No installed app matches bundle ID"),
        ):
            ctrl.get_logs(SIM_UUID, filter="com.nonexistent.app")

    def test_filter_without_dots_passes_through(self) -> None:
        ctrl = IOSController()
        mock_result = MagicMock()
        mock_result.stdout = "log line"
        mock_result.returncode = 0
        with patch.object(ctrl, "_run", return_value=mock_result) as mock_run:
            ctrl.get_logs(SIM_UUID, filter="MyProcess")
        # Should NOT call listapps — no dots means it's already a process name
        calls = [c for c in mock_run.call_args_list if c[0][0] == "listapps"]
        assert len(calls) == 0
        # Should use the filter directly
        log_call = mock_run.call_args_list[0]
        assert 'process == "MyProcess"' in log_call[0]

    def test_get_logs_physical_custom_time_window(self) -> None:
        ctrl = IOSController()
        mock_result = MagicMock()
        mock_result.stdout = "log"
        mock_result.returncode = 0
        with patch.object(ctrl, "_run_log", return_value=mock_result) as mock_run:
            ctrl.get_logs(PHYS_UDID, time_window="1h")
        mock_run.assert_called_once_with(
            "show",
            "--device",
            PHYS_UDID,
            "--last",
            "1h",
            "--style",
            "syslog",
            check=False,
            timeout=30,
        )

    def test_get_logs_physical_with_process_filter(self) -> None:
        ctrl = IOSController()
        mock_result = MagicMock()
        mock_result.stdout = "filtered device log"
        mock_result.returncode = 0
        with patch.object(ctrl, "_run_log", return_value=mock_result) as mock_run:
            result = ctrl.get_logs(PHYS_UDID, filter="MyProcess")
        mock_run.assert_called_once_with(
            "show",
            "--device",
            PHYS_UDID,
            "--last",
            "5m",
            "--style",
            "syslog",
            "--predicate",
            'process == "MyProcess"',
            check=False,
            timeout=30,
        )
        assert result == "filtered device log"

    def test_get_logs_physical_empty_output(self) -> None:
        ctrl = IOSController()
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.returncode = 1
        with patch.object(ctrl, "_run_log", return_value=mock_result):
            result = ctrl.get_logs(PHYS_UDID)
        assert result == ""

    def test_invalid_filter_rejected(self) -> None:
        ctrl = IOSController()
        with pytest.raises(ValueError, match="Invalid filter"):
            ctrl.get_logs(SIM_UUID, filter='bad"; rm -rf /')

    def test_leading_dash_filter_rejected(self) -> None:
        """linc-nerve#128/129 round 1 finding 3: the old `^[\\w.\\-]+$`
        `_FILTER_RE` allowed a leading `-` (`-c` matched: `\\w` covers
        letters/digits/underscore, and `-` is also in the class), so a
        flag-shaped filter value would have reached `get_log_stream_cmd`'s
        subprocess argv unvalidated. Anchoring the first character closes
        that at this defense-in-depth layer too."""
        ctrl = IOSController()
        with pytest.raises(ValueError, match="Invalid filter"):
            ctrl.get_logs(SIM_UUID, filter="-c")


class TestIOSControllerListApps:
    """Characterization tests for IOSController.list_apps (CC=7, CRAP=56)."""

    def test_simulator_list_apps_parses_plist(self) -> None:
        """Simulator path: parses CFBundleIdentifier from OpenStep plist output."""
        ctrl = IOSController()
        plist_output = (
            '    CFBundleIdentifier = "com.apple.mobilesafari";\n'
            "    CFBundleDisplayName = Safari;\n"
            '    CFBundleIdentifier = "com.lincmobile.level";\n'
            "    CFBundleDisplayName = PrecisionLevel;\n"
            '    CFBundleIdentifier = "com.lincmobile.level";\n'  # duplicate
        )
        mock_result = MagicMock()
        mock_result.stdout = plist_output
        mock_result.returncode = 0

        with patch.object(ctrl, "_run", return_value=mock_result):
            apps = ctrl.list_apps(SIM_UUID)

        # Should be sorted and deduplicated
        assert apps == ["com.apple.mobilesafari", "com.lincmobile.level"]

    def test_simulator_list_apps_empty_output(self) -> None:
        """Simulator path: empty stdout -> returns empty list."""
        ctrl = IOSController()
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.returncode = 0

        with patch.object(ctrl, "_run", return_value=mock_result):
            apps = ctrl.list_apps(SIM_UUID)

        assert apps == []

    def test_simulator_list_apps_failed_command(self) -> None:
        """Simulator path: simctl listapps returns non-zero -> returns empty list."""
        ctrl = IOSController()
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.returncode = 1

        with patch.object(ctrl, "_run", return_value=mock_result):
            apps = ctrl.list_apps(SIM_UUID)

        assert apps == []

    def test_physical_list_apps_parses_json(self, tmp_path: Any) -> None:
        """Physical device path: parses bundleIdentifier from devicectl JSON."""
        ctrl = IOSController()
        apps_json = {
            "result": {
                "apps": [
                    {"bundleIdentifier": "com.lincmobile.sniperpulse"},
                    {"bundleIdentifier": "com.apple.mobilesafari"},
                    {"bundleIdentifier": "com.lincmobile.level"},
                ]
            }
        }

        def write_apps_json(*args: Any, **kwargs: Any) -> None:
            """Mock _run_devicectl that writes JSON to the --json-output path."""
            # Find the --json-output arg: it's passed as positional args to _run_devicectl
            # args layout: ("device", "info", "apps", "--device", serial, "--json-output", path)
            arg_list = list(args)
            if "--json-output" in arg_list:
                idx = arg_list.index("--json-output")
                json_path = arg_list[idx + 1]
                with open(json_path, "w") as f:
                    json.dump(apps_json, f)

        with (
            patch.object(ctrl, "_is_physical", return_value=True),
            patch.object(ctrl, "_run_devicectl", side_effect=write_apps_json),
        ):
            apps = ctrl.list_apps(PHYS_UDID)

        assert apps == [
            "com.apple.mobilesafari",
            "com.lincmobile.level",
            "com.lincmobile.sniperpulse",
        ]

    def test_physical_list_apps_json_error(self) -> None:
        """Physical device path: invalid JSON -> returns empty list."""
        ctrl = IOSController()

        def write_bad_json(*args: Any, **kwargs: Any) -> None:
            """Mock _run_devicectl that writes invalid JSON."""
            arg_list = list(args)
            if "--json-output" in arg_list:
                idx = arg_list.index("--json-output")
                json_path = arg_list[idx + 1]
                with open(json_path, "w") as f:
                    f.write("{invalid json")

        with (
            patch.object(ctrl, "_is_physical", return_value=True),
            patch.object(ctrl, "_run_devicectl", side_effect=write_bad_json),
        ):
            apps = ctrl.list_apps(PHYS_UDID)

        assert apps == []


class TestIOSSimulatorLifecycle:
    def test_boot(self) -> None:
        ctrl = IOSController()
        with patch.object(ctrl, "_run") as mock_run:
            ctrl.boot("UUID")
        mock_run.assert_called_once_with("boot", "UUID", check=False)

    def test_shutdown(self) -> None:
        ctrl = IOSController()
        with patch.object(ctrl, "_run") as mock_run:
            ctrl.shutdown("UUID")
        mock_run.assert_called_once_with("shutdown", "UUID", check=False)


def _devicectl_payload(devices: list[dict]) -> dict:
    return {"result": {"devices": devices}}


def _devicectl_device(
    udid: str = "00008110-AAAA0000AAAA0000",
    device_type: str = "iPhone",
    name: str = "Bench iPhone",
    os_version: str = "26.5",
    pairing: str = "paired",
    tunnel: str = "disconnected",
    visibility_class: str | None = None,
    reality: str | None = None,
) -> dict:
    dev = {
        "hardwareProperties": {"udid": udid, "deviceType": device_type},
        "connectionProperties": {"pairingState": pairing, "tunnelState": tunnel},
        "deviceProperties": {"name": name, "osVersionNumber": os_version},
    }
    if visibility_class is not None:
        dev["visibilityClass"] = visibility_class
    if reality is not None:
        dev["hardwareProperties"]["reality"] = reality
    return dev


class TestDevicectlDiscovery:
    """_discover_physical_devicectl — JSON schema verified 2026-06-09, Xcode 26.x."""

    def _discover(self, payload: dict) -> list:
        ctrl = IOSController()

        def fake_run(cmd: list, **kwargs: Any) -> MagicMock:
            out_idx = cmd.index("--json-output") + 1
            with open(cmd[out_idx], "w") as f:
                json.dump(payload, f)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            return ctrl._discover_physical_devicectl()

    def test_paired_disconnected_is_online(self) -> None:
        devices = self._discover(_devicectl_payload([_devicectl_device()]))
        assert len(devices) == 1
        assert devices[0].serial == "00008110-AAAA0000AAAA0000"
        assert devices[0].state == "online"
        assert devices[0].device_type == DeviceType.PHONE
        assert devices[0].model == "Bench iPhone"
        assert devices[0].os_version == "26.5"

    def test_paired_unavailable_is_offline(self) -> None:
        devices = self._discover(_devicectl_payload([_devicectl_device(tunnel="unavailable")]))
        assert devices[0].state == "offline"

    def test_unknown_tunnel_state_is_offline(self) -> None:
        devices = self._discover(_devicectl_payload([_devicectl_device(tunnel="connecting")]))
        assert devices[0].state == "offline"

    def test_unpaired_is_offline(self) -> None:
        devices = self._discover(
            _devicectl_payload([_devicectl_device(pairing="unpaired", tunnel="connected")])
        )
        assert devices[0].state == "offline"

    def test_watch_and_ipad_device_types(self) -> None:
        devices = self._discover(
            _devicectl_payload(
                [
                    _devicectl_device(udid="00008310-W1", device_type="appleWatch"),
                    _devicectl_device(udid="00008120-T1", device_type="iPad"),
                ]
            )
        )
        types = {d.serial: d.device_type for d in devices}
        assert types == {
            "00008310-W1": DeviceType.WATCH,
            "00008120-T1": DeviceType.TABLET,
        }

    def test_device_type_substring_case_insensitive(self) -> None:
        """Apple has shipped dotted type strings; match by substring."""
        devices = self._discover(
            _devicectl_payload(
                [_devicectl_device(udid="00008310-W2", device_type="com.apple.Watch.Series9")]
            )
        )
        assert devices[0].device_type == DeviceType.WATCH

    def test_unknown_device_type_skipped(self) -> None:
        devices = self._discover(
            _devicectl_payload(
                [
                    _devicectl_device(udid="00008300-TV1", device_type="appleTV"),
                    _devicectl_device(udid="00008110-P1", device_type="iPhone"),
                ]
            )
        )
        assert [d.serial for d in devices] == ["00008110-P1"]

    def test_duplicate_udid_deduped_prefer_connected(self) -> None:
        """Same device over USB + network: keep the better tunnel state."""
        devices = self._discover(
            _devicectl_payload(
                [
                    _devicectl_device(udid="00008110-DUP", tunnel="disconnected"),
                    _devicectl_device(udid="00008110-DUP", tunnel="connected"),
                ]
            )
        )
        assert len(devices) == 1
        assert devices[0].state == "online"

    def test_empty_devices_is_valid_no_devices(self) -> None:
        assert self._discover(_devicectl_payload([])) == []


WATCH_SIM_UDID = "35FE5643-28E2-452F-B430-BB5D16ED862A"


class TestDevicectlSimulatorSkip:
    """devicectl on Xcode 27 also lists simulators; they must not be physical."""

    def _ctrl_discover(self, ctrl: IOSController, payload: dict) -> list:
        def fake_run(cmd: list, **kwargs: Any) -> MagicMock:
            out_idx = cmd.index("--json-output") + 1
            with open(cmd[out_idx], "w") as f:
                json.dump(payload, f)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            return ctrl._discover_physical_devicectl()

    def test_watch_simulator_skipped_iphone_kept(self) -> None:
        ctrl = IOSController()
        payload = _devicectl_payload(
            [
                _devicectl_device(visibility_class="default", reality="physical"),
                _devicectl_device(
                    udid=WATCH_SIM_UDID,
                    device_type="appleWatch",
                    visibility_class="simulators",
                    reality="simulated",
                ),
            ]
        )
        devices = self._ctrl_discover(ctrl, payload)
        assert [d.serial for d in devices] == ["00008110-AAAA0000AAAA0000"]
        assert WATCH_SIM_UDID not in ctrl._physical_serials

    def test_visibility_class_only_skipped(self) -> None:
        ctrl = IOSController()
        payload = _devicectl_payload(
            [_devicectl_device(udid=WATCH_SIM_UDID, visibility_class="simulators")]
        )
        assert self._ctrl_discover(ctrl, payload) == []
        assert WATCH_SIM_UDID not in ctrl._physical_serials

    def test_reality_only_skipped(self) -> None:
        ctrl = IOSController()
        payload = _devicectl_payload([_devicectl_device(udid=WATCH_SIM_UDID, reality="simulated")])
        assert self._ctrl_discover(ctrl, payload) == []
        assert WATCH_SIM_UDID not in ctrl._physical_serials

    def test_unavailable_physical_ipad_without_reality_still_returned(self) -> None:
        ctrl = IOSController()
        payload = _devicectl_payload(
            [
                _devicectl_device(
                    udid="00008027-BBBB0000BBBB0000",
                    device_type="iPad",
                    pairing="unpaired",
                    visibility_class="default",
                )
            ]
        )
        devices = self._ctrl_discover(ctrl, payload)
        assert [d.serial for d in devices] == ["00008027-BBBB0000BBBB0000"]
        assert devices[0].state == "offline"

    def _discover_dedup(self, ctrl: IOSController) -> list:
        simctl_output = json.dumps(
            {
                "devices": {
                    "com.apple.CoreSimulator.SimRuntime.watchOS-11-0": [
                        {
                            "name": "Apple Watch Series 10 (46mm)",
                            "udid": WATCH_SIM_UDID,
                            "state": "Booted",
                            "isAvailable": True,
                        }
                    ]
                }
            }
        )
        payload = _devicectl_payload(
            [
                _devicectl_device(
                    udid=WATCH_SIM_UDID,
                    device_type="appleWatch",
                    visibility_class="simulators",
                    reality="simulated",
                )
            ]
        )

        def fake_run(cmd: list, **kwargs: Any) -> MagicMock:
            out_idx = cmd.index("--json-output") + 1
            with open(cmd[out_idx], "w") as f:
                json.dump(payload, f)
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch.object(ctrl, "_run", return_value=MagicMock(stdout=simctl_output)),
            patch("subprocess.run", side_effect=fake_run),
        ):
            return ctrl.discover()

    def test_discover_lists_simulator_once(self) -> None:
        ctrl = IOSController()
        devices = self._discover_dedup(ctrl)
        assert [d.serial for d in devices] == [WATCH_SIM_UDID]
        assert devices[0].device_type == DeviceType.WATCH
        assert devices[0].model == "Apple Watch Series 10 (46mm)"

    def test_screenshot_after_discovery_takes_simulator_path(self) -> None:
        ctrl = IOSController()
        self._discover_dedup(ctrl)
        with (
            patch.object(ctrl, "_screenshot_simulator", return_value=b"SIMPNG") as sim,
            patch.object(ctrl, "_require_wda") as require_wda,
            patch.object(ctrl, "_screenshot_physical") as physical,
        ):
            assert ctrl.screenshot(WATCH_SIM_UDID) == b"SIMPNG"
        sim.assert_called_once()
        require_wda.assert_not_called()
        physical.assert_not_called()


class TestDiscoverPhysicalFallback:
    """_discover_physical — devicectl first, xctrace fallback."""

    def test_devicectl_success_no_xctrace(self) -> None:
        ctrl = IOSController()
        sentinel = [MagicMock()]
        with (
            patch.object(ctrl, "_discover_physical_devicectl", return_value=sentinel),
            patch.object(ctrl, "_discover_physical_xctrace") as mock_xctrace,
        ):
            assert ctrl._discover_physical() == sentinel
            mock_xctrace.assert_not_called()

    def test_missing_binary_falls_back(self) -> None:
        ctrl = IOSController()
        sentinel = [MagicMock()]
        with (
            patch.object(
                ctrl,
                "_discover_physical_devicectl",
                side_effect=FileNotFoundError("no devicectl"),
            ),
            patch.object(ctrl, "_discover_physical_xctrace", return_value=sentinel),
        ):
            assert ctrl._discover_physical() == sentinel

    def test_nonzero_exit_falls_back(self) -> None:
        import subprocess as sp

        ctrl = IOSController()
        with (
            patch.object(
                ctrl,
                "_discover_physical_devicectl",
                side_effect=sp.CalledProcessError(1, "devicectl"),
            ),
            patch.object(ctrl, "_discover_physical_xctrace", return_value=[]) as mock_x,
        ):
            ctrl._discover_physical()
            mock_x.assert_called_once()

    def test_malformed_json_warns_and_falls_back(self) -> None:
        ctrl = IOSController()

        def fake_run(cmd: list, **kwargs: Any) -> MagicMock:
            out_idx = cmd.index("--json-output") + 1
            with open(cmd[out_idx], "w") as f:
                f.write("{not json")
            return MagicMock(returncode=0)

        with (
            patch("subprocess.run", side_effect=fake_run),
            patch.object(ctrl, "_discover_physical_xctrace", return_value=[]) as mock_x,
            patch("linc_nerve.core.ios.logger") as mock_logger,
        ):
            ctrl._discover_physical()
            mock_x.assert_called_once()
            mock_logger.warning.assert_called_once()

    def test_tmpfile_cleaned_up(self) -> None:
        from pathlib import Path

        ctrl = IOSController()
        written: list[str] = []

        def fake_run(cmd: list, **kwargs: Any) -> MagicMock:
            out_idx = cmd.index("--json-output") + 1
            written.append(cmd[out_idx])
            with open(cmd[out_idx], "w") as f:
                json.dump(_devicectl_payload([]), f)
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=fake_run):
            ctrl._discover_physical_devicectl()
        assert written and not Path(written[0]).exists()


# --- Task 6: IOSController MJPEG screenshot fast path ---


def _ios_ctrl():
    return IOSController()


def test_screenshot_uses_warm_mjpeg_when_available():
    ctrl = _ios_ctrl()
    session = MagicMock(mjpeg_port=9100)
    with (
        patch.object(ctrl, "_is_physical", return_value=False),
        patch.object(ctrl, "_get_wda", return_value=session),
        patch.object(ctrl._mjpeg, "screenshot", return_value=b"MJPEGPNG") as shot,
    ):
        out = ctrl.screenshot("sim-serial", full_resolution=True)
    assert out == b"MJPEGPNG"
    shot.assert_called_once_with("sim-serial", 9100)


def test_screenshot_falls_back_to_simctl_when_mjpeg_raises():
    ctrl = _ios_ctrl()
    session = MagicMock(mjpeg_port=9100)
    with (
        patch.object(ctrl, "_is_physical", return_value=False),
        patch.object(ctrl, "_get_wda", return_value=session),
        patch.object(ctrl._mjpeg, "screenshot", side_effect=RuntimeError("cold")),
        patch.object(ctrl, "_screenshot_simctl", return_value=b"SIMCTLPNG") as fb,
    ):
        out = ctrl.screenshot("sim-serial")
    assert out == b"SIMCTLPNG"
    fb.assert_called_once()


def test_screenshot_skips_mjpeg_when_port_none():
    ctrl = _ios_ctrl()
    session = MagicMock(mjpeg_port=None)
    with (
        patch.object(ctrl, "_is_physical", return_value=False),
        patch.object(ctrl, "_get_wda", return_value=session),
        patch.object(ctrl._mjpeg, "screenshot") as shot,
        patch.object(ctrl, "_screenshot_simctl", return_value=b"SIMCTLPNG"),
    ):
        out = ctrl.screenshot("sim-serial")
    assert out == b"SIMCTLPNG"
    shot.assert_not_called()


def test_cleanup_stops_mjpeg_streams():
    ctrl = _ios_ctrl()
    with patch.object(ctrl._wda, "stop_all"), patch.object(ctrl._mjpeg, "stop_all") as mjpeg_stop:
        ctrl.cleanup()
    mjpeg_stop.assert_called_once()


# --- Task 7: iOS-26 simctl fix (abf68b2c) ---


def test_screenshot_simctl_writes_to_file_not_stdout():
    ctrl = IOSController()
    written = {}

    def fake_run_binary(*args, timeout=15):
        # args == ("io", serial, "screenshot", "--type=png", <path>)
        path = args[-1]
        assert path != "-", "must not use stdout '-' (rejected on iOS 26)"
        with open(path, "wb") as f:
            f.write(b"FILEPNG")
        written["path"] = path
        return MagicMock(stdout=b"")

    with patch.object(ctrl, "_run_binary", side_effect=fake_run_binary):
        out = ctrl._screenshot_simctl("sim-serial")
    assert out == b"FILEPNG"
    assert not os.path.exists(written["path"]), "temp file must be unlinked"


SIM_UUID = "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"
HW_UDID = "00008110-0000000000000001"


class TestClassifyPhysical:
    def test_simulator_set_wins_over_physical_set(self) -> None:
        assert _classify_physical(SIM_UUID, {SIM_UUID}, {SIM_UUID}) is False

    def test_simulator_set_wins_over_hardware_shaped_udid(self) -> None:
        assert _classify_physical(HW_UDID, set(), {HW_UDID}) is False

    def test_physical_set_wins_when_not_simulator(self) -> None:
        assert _classify_physical(SIM_UUID, {SIM_UUID}, set()) is True

    def test_falls_back_to_udid_shape(self) -> None:
        assert _classify_physical(HW_UDID, set(), set()) is True
        assert _classify_physical(SIM_UUID, set(), set()) is False


class TestIsPhysicalPrecedence:
    def test_udid_in_both_sets_is_not_physical(self) -> None:
        ctrl = IOSController()
        ctrl._physical_serials.add(SIM_UUID)
        ctrl._simulator_serials.add(SIM_UUID)
        assert ctrl._is_physical(SIM_UUID) is False

    def test_only_physical_set_is_physical(self) -> None:
        ctrl = IOSController()
        ctrl._physical_serials.add(SIM_UUID)
        assert ctrl._is_physical(SIM_UUID) is True

    def test_unseen_uuid_is_not_physical(self) -> None:
        assert IOSController()._is_physical(SIM_UUID) is False

    def test_unseen_hardware_udid_is_physical(self) -> None:
        assert IOSController()._is_physical(HW_UDID) is True

    def test_discover_simulators_records_udids(self) -> None:
        ctrl = IOSController()
        payload = {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-17-5": [
                    {"udid": SIM_UUID, "name": "iPhone 15", "state": "Booted"},
                    {"udid": "AAAA-BBBB", "name": "iPad Air", "state": "Shutdown"},
                    {"name": "no udid"},
                ]
            }
        }
        fake = MagicMock(stdout=json.dumps(payload))
        with patch.object(ctrl, "_run", return_value=fake):
            devs = ctrl._discover_simulators()
        assert len(devs) == 2
        assert ctrl._simulator_serials == {SIM_UUID, "AAAA-BBBB"}


def test_start_recording_writes_under_linc_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    custom_home = tmp_path / "custom_linc"
    monkeypatch.setenv("LINC_HOME", str(custom_home))

    ctrl = IOSController()
    ctrl._simulator_serials.add(SIM_UUID)

    devices_json = json.dumps(
        {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-17-5": [
                    {"udid": SIM_UUID, "name": "iPhone 15", "state": "Booted"},
                ]
            }
        }
    )

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with (
        patch.object(ctrl, "_run", return_value=MagicMock(stdout=devices_json)),
        patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        patch("time.sleep"),
    ):
        ctrl.start_recording(SIM_UUID)

    mock_popen.assert_called_once()
    recorded_path = ctrl._recording_paths[SIM_UUID]
    expected_dir = custom_home / "nerve" / "recordings"
    assert str(expected_dir) in recorded_path
    assert expected_dir.exists()
