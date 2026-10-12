"""Smoke tests to eliminate CRAP warnings on low-complexity, zero-cov functions."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from linc_nerve.core.ios import IOSController


# ── IOSController.press_key ─────────────────────────────────────────
class TestIOSPressKey:
    def _mk_ctrl(self) -> IOSController:
        ctrl = IOSController()
        return ctrl

    def test_physical_home_uses_wda_press_button(self) -> None:
        ctrl = self._mk_ctrl()
        ctrl._physical_serials.add("PHYS-1")

        mock_session = MagicMock()
        with patch.object(ctrl, "_get_wda", return_value=mock_session):
            ctrl.press_key("PHYS-1", "home")

        mock_session._request.assert_called_once_with(
            "POST", "/wda/pressButton", json={"name": "home"}
        )

    def test_physical_non_home_is_logged_and_skipped(self) -> None:
        ctrl = self._mk_ctrl()
        ctrl._physical_serials.add("PHYS-1")

        with patch.object(ctrl, "_get_wda") as get_wda:
            ctrl.press_key("PHYS-1", "back")
        get_wda.assert_not_called()

    def test_simulator_home_uses_notifyutil(self) -> None:
        ctrl = self._mk_ctrl()
        sim_udid = "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"
        with patch.object(ctrl, "_run") as run:
            ctrl.press_key(sim_udid, "HOME")
        run.assert_called_once()
        call_args = run.call_args[0]
        assert "notifyutil" in call_args
        assert any("springboard" in a for a in call_args)

    def test_simulator_back_maps_to_shake(self) -> None:
        ctrl = self._mk_ctrl()
        sim_udid = "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"
        with patch.object(ctrl, "_run") as run:
            ctrl.press_key(sim_udid, "back")
        assert run.call_args[0][-1] == "shake"

    def test_simulator_unsupported_key_tolerates_error(self) -> None:
        import subprocess

        ctrl = self._mk_ctrl()
        sim_udid = "4C215E09-3AAF-498F-B8AF-FE9F8B3B5177"
        with patch.object(
            ctrl,
            "_run",
            side_effect=subprocess.CalledProcessError(1, ["xcrun"]),
        ):
            # Should swallow and log, not raise
            ctrl.press_key(sim_udid, "weird_key")


# ── IOSController.get_screen_size / get_orientation ─────────────────
class TestIOSDeviceInfo:
    def test_screen_size_from_active_wda(self) -> None:
        ctrl = IOSController()
        mock_session = MagicMock()
        mock_session.is_running = True
        mock_session.screen_size.return_value = {"width": 390, "height": 844}
        ctrl._wda._sessions["UDID-1"] = mock_session

        assert ctrl.get_screen_size("UDID-1") == {"width": 390, "height": 844}

    def test_screen_size_falls_back_when_no_session(self) -> None:
        ctrl = IOSController()
        assert ctrl.get_screen_size("UDID-missing") == {"width": 0, "height": 0}

    def test_screen_size_falls_back_when_session_raises(self) -> None:
        ctrl = IOSController()
        mock_session = MagicMock()
        mock_session.is_running = True
        mock_session.screen_size.side_effect = RuntimeError("wda down")
        ctrl._wda._sessions["UDID-2"] = mock_session

        assert ctrl.get_screen_size("UDID-2") == {"width": 0, "height": 0}

    def test_orientation_from_active_wda(self) -> None:
        ctrl = IOSController()
        mock_session = MagicMock()
        mock_session.is_running = True
        mock_session.orientation.return_value = "landscape"
        ctrl._wda._sessions["UDID-1"] = mock_session

        assert ctrl.get_orientation("UDID-1") == "landscape"

    def test_orientation_default_when_no_session(self) -> None:
        ctrl = IOSController()
        assert ctrl.get_orientation("UDID-missing") == "portrait"

    def test_orientation_default_when_session_raises(self) -> None:
        ctrl = IOSController()
        mock_session = MagicMock()
        mock_session.is_running = True
        mock_session.orientation.side_effect = RuntimeError("wda down")
        ctrl._wda._sessions["UDID-2"] = mock_session

        assert ctrl.get_orientation("UDID-2") == "portrait"


# ── IOSController._resolve_bundle_id_physical ───────────────────────
class TestResolveBundleIdPhysical:
    def test_resolves_executable_name(self, tmp_path) -> None:
        ctrl = IOSController()
        apps_json = {
            "result": {
                "apps": [
                    {
                        "bundleIdentifier": "com.linc.forge",
                        "executableName": "forge",
                    }
                ]
            }
        }

        def fake_devicectl(*args, **kwargs):
            # The function writes to json_path then reads back
            # Simulate by writing the JSON to the tempfile
            json_path = args[args.index("--json-output") + 1]
            with open(json_path, "w") as f:
                json.dump(apps_json, f)
            return MagicMock()

        with patch.object(ctrl, "_run_devicectl", side_effect=fake_devicectl):
            exe = ctrl._resolve_bundle_id_physical("SERIAL", "com.linc.forge")
        assert exe == "forge"

    def test_fallback_to_last_bundle_component(self, tmp_path) -> None:
        ctrl = IOSController()
        apps_json = {
            "result": {"apps": [{"bundleIdentifier": "com.linc.forge", "executableName": ""}]}
        }

        def fake_devicectl(*args, **kwargs):
            json_path = args[args.index("--json-output") + 1]
            with open(json_path, "w") as f:
                json.dump(apps_json, f)
            return MagicMock()

        with patch.object(ctrl, "_run_devicectl", side_effect=fake_devicectl):
            exe = ctrl._resolve_bundle_id_physical("SERIAL", "com.linc.forge")
        assert exe == "forge"

    def test_raises_when_no_app_matches(self) -> None:
        ctrl = IOSController()
        empty = {"result": {"apps": []}}

        def fake_devicectl(*args, **kwargs):
            json_path = args[args.index("--json-output") + 1]
            with open(json_path, "w") as f:
                json.dump(empty, f)
            return MagicMock()

        with (
            patch.object(ctrl, "_run_devicectl", side_effect=fake_devicectl),
            pytest.raises(ValueError, match="No installed app matches"),
        ):
            ctrl._resolve_bundle_id_physical("SERIAL", "com.linc.forge")


# ── scrcpy.ScrcpyManager.screenshot ─────────────────────────────────
class TestScrcpyManagerScreenshot:
    def test_screenshot_happy_path(self) -> None:
        from linc_nerve.core.scrcpy import ScrcpyManager

        mgr = ScrcpyManager()
        mock_stream = MagicMock()
        mock_stream.screenshot.return_value = b"PNGDATA"
        with (
            patch.object(mgr, "is_open", return_value=False),
            patch.object(mgr, "start_stream", return_value=mock_stream),
            patch.object(mgr, "record_success") as ok,
        ):
            data = mgr.screenshot("SERIAL")
        assert data == b"PNGDATA"
        ok.assert_called_once()

    def test_screenshot_raises_when_circuit_open(self) -> None:
        from linc_nerve.core.scrcpy import ScrcpyManager

        mgr = ScrcpyManager()
        with (
            patch.object(mgr, "is_open", return_value=True),
            pytest.raises(RuntimeError, match="circuit open"),
        ):
            mgr.screenshot("SERIAL")

    def test_screenshot_retries_once_on_runtime_error(self) -> None:
        from linc_nerve.core.scrcpy import ScrcpyManager

        mgr = ScrcpyManager()
        mock_stream = MagicMock()
        mock_stream.screenshot.side_effect = [RuntimeError("no frame"), b"RETRYPNG"]

        with (
            patch.object(mgr, "is_open", return_value=False),
            patch.object(mgr, "start_stream", return_value=mock_stream),
            patch.object(mgr, "stop_stream") as stop,
            patch.object(mgr, "record_success"),
            patch.object(mgr, "record_failure"),
        ):
            data = mgr.screenshot("SERIAL")
        assert data == b"RETRYPNG"
        stop.assert_called_once_with("SERIAL")


# ── WDASession.stop / WDAManager.get_or_start ──────────────────────
class TestWDA:
    def test_session_stop_tolerates_partial_state(self) -> None:
        from linc_nerve.core.wda import WDASession

        sess = WDASession("SERIAL", 8100, is_physical=False)
        sess._session_id = "sid"
        mock_client = MagicMock()
        sess._client = mock_client
        sess._process = MagicMock()
        sess._process.kill.side_effect = OSError("already dead")
        sess._process.wait = MagicMock()
        sess._tmp_xctestrun = None

        # Should not raise
        sess.stop()
        assert sess._session_id is None
        mock_client.close.assert_called_once()
        assert sess._client is None
        assert sess._process is None

    def test_session_stop_no_client_no_process(self) -> None:
        from linc_nerve.core.wda import WDASession

        sess = WDASession("SERIAL", 8100, is_physical=False)
        sess._client = None
        sess._process = None
        sess._tmp_xctestrun = None
        # Should not raise
        sess.stop()

    def test_manager_get_or_start_returns_healthy_existing(self) -> None:
        from linc_nerve.core.wda import WDAManager

        mgr = WDAManager()
        existing = MagicMock()
        existing.is_running = True
        existing._is_healthy.return_value = True
        mgr._sessions["SERIAL"] = existing

        out = mgr.get_or_start("SERIAL")
        assert out is existing

    def test_manager_get_or_start_replaces_dead_session(self) -> None:
        from linc_nerve.core.wda import WDAManager

        mgr = WDAManager()
        dead = MagicMock()
        dead.is_running = False
        dead._is_healthy.return_value = False
        mgr._sessions["SERIAL"] = dead

        fake_new = MagicMock()
        with (
            patch("linc_nerve.core.wda._allocate_port", return_value=9000),
            patch("linc_nerve.core.wda.WDASession", return_value=fake_new),
        ):
            out = mgr.get_or_start("SERIAL")

        dead.stop.assert_called_once()
        fake_new.start.assert_called_once()
        assert mgr._sessions["SERIAL"] is fake_new
        assert out is fake_new

    def test_manager_get_or_start_fresh_session(self) -> None:
        from linc_nerve.core.wda import WDAManager

        mgr = WDAManager()  # no sessions
        fake_new = MagicMock()
        with (
            patch("linc_nerve.core.wda._allocate_port", return_value=9100),
            patch("linc_nerve.core.wda.WDASession", return_value=fake_new),
        ):
            out = mgr.get_or_start("NEW-SERIAL", is_physical=True)

        fake_new.start.assert_called_once()
        assert out is fake_new
