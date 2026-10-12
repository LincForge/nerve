"""Tests for Android controller."""

from __future__ import annotations

import io
import subprocess
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest
import structlog.testing

from linc_nerve.core.adb_path import ADB_PATH
from linc_nerve.core.android import AndroidController
from linc_nerve.core.device import (
    DeviceError,
    DeviceType,
    Platform,
    UiHierarchyUnavailableError,
)


def _make_controller() -> AndroidController:
    """Create a controller with scrcpy disabled for unit tests."""
    ctrl = AndroidController()
    ctrl._scrcpy_available = False  # Skip scrcpy in unit tests
    return ctrl


class _FakeAdbConnection:
    """Fakes adbutils' ``AdbConnection`` wire protocol for ``host:devices``.

    Real ``adb devices`` output separates serial and state with a TAB, and an
    mDNS wifi-adb serial can itself contain a space — the reason
    ``AndroidController.discover`` no longer routes through
    ``AdbClient.device_list()`` (which splits on ANY whitespace upstream) and
    instead talks to the transport service directly via
    ``adb_health.parse_adb_devices``.
    """

    def __init__(self, payload: str, sent: list[str]) -> None:
        self._payload = payload
        self._sent = sent

    def __enter__(self) -> _FakeAdbConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def send_command(self, cmd: str) -> None:
        self._sent.append(cmd)

    def check_okay(self) -> None:
        pass

    def read_string_block(self) -> str:
        return self._payload


def _fake_client(payload: str, devices_by_serial: dict[str, Any] | None = None) -> MagicMock:
    """A MagicMock adbutils client wired for the ``host:devices`` wire path."""
    sent: list[str] = []
    client = MagicMock()
    client.make_connection.return_value = _FakeAdbConnection(payload, sent)
    client.sent_commands = sent
    devices_by_serial = devices_by_serial or {}
    client.device.side_effect = lambda serial: devices_by_serial[serial]
    return client


def _sh_tokenize(cmd: str) -> list[str]:
    """Run ``cmd`` through the local ``/bin/sh`` exactly as adbutils' ``device.shell``
    hands a raw string to the real device shell, and return the argv tokens a
    trailing command would actually see.

    Prepends ``printf "%s\\n" `` (unquoted) so the shell's own word-splitting /
    control-operator rules (unquoted spaces, ``&`` backgrounding, ``;`` command
    separation, etc.) run over ``cmd`` exactly as they would on-device, then
    reads back one argv token per output line. This is deliberately NOT
    ``shlex.split`` — shlex doesn't reproduce sh's control-operator semantics
    (e.g. an unquoted ``&`` splits the command instead of just being "a
    character"), which is precisely the corruption this bug produces.
    """
    result = subprocess.run(
        ["sh", "-c", 'printf "%s\\n" ' + cmd],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return [line for line in result.stdout.split("\n") if line != ""]


class TestAndroidDiscovery:
    def test_discover_phone(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.serial = "PIXEL8PRO"
        mock_device.shell.side_effect = lambda cmd, **kw: {
            "getprop ro.product.model": "Pixel 8 Pro",
            "getprop ro.build.version.release": "14",
            "getprop ro.build.characteristics": "default",
        }.get(cmd, "")

        mock_client = _fake_client("PIXEL8PRO\tdevice\n", {"PIXEL8PRO": mock_device})
        ctrl._client = mock_client

        devices = ctrl.discover()
        assert len(devices) == 1
        assert devices[0].serial == "PIXEL8PRO"
        assert devices[0].device_type == DeviceType.PHONE
        assert devices[0].model == "Pixel 8 Pro"
        assert devices[0].platform == Platform.ANDROID
        assert mock_client.sent_commands == ["host:devices"]

    def test_discover_emulator(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.serial = "emulator-5554"
        mock_device.shell.side_effect = lambda cmd, **kw: {
            "getprop ro.product.model": "sdk_gphone64",
            "getprop ro.build.version.release": "15",
            "getprop ro.build.characteristics": "emulator",
        }.get(cmd, "")

        mock_client = _fake_client("emulator-5554\tdevice\n", {"emulator-5554": mock_device})
        ctrl._client = mock_client

        devices = ctrl.discover()
        assert devices[0].device_type == DeviceType.EMULATOR

    def test_discover_watch(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.serial = "WATCH123"
        mock_device.shell.side_effect = lambda cmd, **kw: {
            "getprop ro.product.model": "Pixel Watch 2",
            "getprop ro.build.version.release": "14",
            "getprop ro.build.characteristics": "watch",
        }.get(cmd, "")

        mock_client = _fake_client("WATCH123\tdevice\n", {"WATCH123": mock_device})
        ctrl._client = mock_client

        devices = ctrl.discover()
        assert devices[0].device_type == DeviceType.WATCH

    def test_discover_no_devices(self) -> None:
        ctrl = AndroidController()
        mock_client = _fake_client("List of devices attached\n\n")
        ctrl._client = mock_client

        assert ctrl.discover() == []

    def test_discover_adb_unavailable(self) -> None:
        ctrl = AndroidController()
        mock_client = MagicMock()
        mock_client.make_connection.side_effect = FileNotFoundError("adb not found")
        ctrl._client = mock_client

        assert ctrl.discover() == []

    def test_discover_spaced_mdns_serial_survives_and_is_a_watch(self) -> None:
        """The reactor case: an mDNS wifi-adb serial with a collision suffix
        (`adb-RFAT629KQ9A-x (2)._adb-tls-connect._tcp`) must survive discovery
        intact — not get truncated to its state token and dropped."""
        mdns_serial = "adb-RFAT629KQ9A-x (2)._adb-tls-connect._tcp"
        phone_device = MagicMock()
        phone_device.serial = "R3CFAKE00001"
        phone_device.shell.side_effect = lambda cmd, **kw: {
            "getprop ro.product.model": "Pixel 8 Pro",
            "getprop ro.build.version.release": "14",
            "getprop ro.build.characteristics": "default",
        }.get(cmd, "")

        watch_device = MagicMock()
        watch_device.serial = mdns_serial
        watch_device.shell.side_effect = lambda cmd, **kw: {
            "getprop ro.product.model": "Galaxy Watch7",
            "getprop ro.build.version.release": "14",
            "getprop ro.build.characteristics": "watch",
        }.get(cmd, "")

        payload = f"R3CFAKE00001\tdevice\n{mdns_serial}\tdevice\n"
        mock_client = _fake_client(
            payload, {"R3CFAKE00001": phone_device, mdns_serial: watch_device}
        )

        ctrl = AndroidController()
        ctrl._client = mock_client

        devices = ctrl.discover()

        assert len(devices) == 2
        assert mock_client.sent_commands == ["host:devices"]
        by_serial = {d.serial: d for d in devices}
        assert by_serial["R3CFAKE00001"].device_type == DeviceType.PHONE
        assert by_serial[mdns_serial].serial == mdns_serial
        assert by_serial[mdns_serial].device_type == DeviceType.WATCH


class TestAndroidScreenshot:
    def test_screenshot_via_adbutils(self) -> None:
        ctrl = _make_controller()
        fake_img = MagicMock()
        buf = io.BytesIO()
        # Write a fake PNG header + enough data to pass size check
        buf.write(b"\x89PNG" + b"\x00" * 30_000)
        fake_png = buf.getvalue()

        def save_side_effect(buf_arg: io.BytesIO, format: str = "PNG") -> None:
            buf_arg.write(fake_png)

        fake_img.save = save_side_effect

        mock_device = MagicMock()
        mock_device.screenshot.return_value = fake_img
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        data = ctrl.screenshot("PIXEL8PRO")
        assert len(data) >= 30_000

    def test_screenshot_emulator_fallback(self) -> None:
        ctrl = _make_controller()

        # adbutils screenshot returns tiny image
        fake_img = MagicMock()

        def save_tiny(buf: io.BytesIO, format: str = "PNG") -> None:
            buf.write(b"\x89PNG" + b"\x00" * 100)

        fake_img.save = save_tiny

        mock_device = MagicMock()
        mock_device.screenshot.return_value = fake_img
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        large_png = b"\x89PNG" + b"\x00" * 50_000

        def subprocess_side_effect(cmd: list, **kwargs: object) -> MagicMock:
            if "screencap" in cmd:
                return MagicMock(stdout=b"\x89PNG" + b"\x00" * 100)
            if "emu" in cmd:
                return MagicMock(stdout=b"", returncode=0)
            if "cat" in cmd:
                return MagicMock(stdout=large_png)
            return MagicMock(stdout=b"", returncode=0)

        with patch("subprocess.run", side_effect=subprocess_side_effect):
            data = ctrl.screenshot("emulator-5554")

        assert len(data) == len(large_png)

    def test_screenshot_physical_no_fallback(self) -> None:
        ctrl = _make_controller()
        small_png = b"\x89PNG" + b"\x00" * 100

        fake_img = MagicMock()

        def save_small(buf: io.BytesIO, format: str = "PNG") -> None:
            buf.write(small_png)

        fake_img.save = save_small

        mock_device = MagicMock()
        mock_device.screenshot.return_value = fake_img
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        # Physical device — should NOT trigger fallback regardless of size
        data = ctrl.screenshot("PIXEL8PRO")
        assert len(data) == len(small_png)

    def test_screenshot_scrcpy_does_not_hold_device_lock(self) -> None:
        """scrcpy fast path must NOT hold _device_lock — another thread
        should be able to acquire it concurrently."""
        import threading

        ctrl = _make_controller()
        lock_acquired = threading.Event()
        scrcpy_called = threading.Event()

        fake_scrcpy = MagicMock()
        fake_scrcpy.is_open.return_value = False

        def slow_screenshot(serial: str) -> bytes:
            scrcpy_called.set()
            # Block until the other thread signals it got the lock
            lock_acquired.wait(timeout=5)
            return b"\x89PNG" + b"\x00" * 30_000

        fake_scrcpy.screenshot.side_effect = slow_screenshot
        ctrl._scrcpy = fake_scrcpy
        ctrl._scrcpy_available = True

        errors: list[str] = []

        def take_screenshot() -> None:
            try:
                ctrl.screenshot("PIXEL8PRO")
            except Exception as exc:
                errors.append(str(exc))

        def try_lock() -> None:
            scrcpy_called.wait(timeout=5)
            # The device lock should be FREE while scrcpy is running
            lock = ctrl._device_lock("PIXEL8PRO")
            acquired = lock.acquire(timeout=2)
            if acquired:
                lock_acquired.set()
                lock.release()
            else:
                errors.append("Could not acquire device lock while scrcpy running")
                lock_acquired.set()  # Unblock anyway

        t1 = threading.Thread(target=take_screenshot)
        t2 = threading.Thread(target=try_lock)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Lock contention: {errors}"


class TestAndroidInput:
    def test_tap(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.tap("ABC", 100, 200)
        mock_device.shell.assert_called_once_with("input tap 100 200", timeout=15)

    def test_long_press(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.long_press("ABC", 100, 200)
        mock_device.shell.assert_called_once_with("input swipe 100 200 100 200 500", timeout=15)

    def test_long_press_custom_duration(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.long_press("ABC", 50, 75, duration_ms=1000)
        mock_device.shell.assert_called_once_with("input swipe 50 75 50 75 1000", timeout=15)

    def test_swipe(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.swipe("ABC", 10, 20, 300, 400, 500)
        mock_device.shell.assert_called_once_with("input swipe 10 20 300 400 500", timeout=15)

    # Defence in depth (audit 2026-09-30 F1, bead linc-nerve-df3.1): the
    # shell sinks cast with int() so a non-integer that bypasses the daemon's
    # typed-param coercion raises instead of being interpolated verbatim.
    @staticmethod
    def _ctrl_with_device() -> tuple[AndroidController, MagicMock]:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client
        return ctrl, mock_device

    def test_tap_rejects_non_integer_coordinate_before_shell(self) -> None:
        ctrl, mock_device = self._ctrl_with_device()
        with pytest.raises(ValueError):
            ctrl.tap("ABC", "1 MARKER", 2)  # type: ignore[arg-type]
        mock_device.shell.assert_not_called()

    def test_tap_rejects_non_integer_y_before_shell(self) -> None:
        ctrl, mock_device = self._ctrl_with_device()
        with pytest.raises(ValueError):
            ctrl.tap("ABC", 1, "2; id")  # type: ignore[arg-type]
        mock_device.shell.assert_not_called()

    def test_swipe_rejects_non_integer_duration_before_shell(self) -> None:
        ctrl, mock_device = self._ctrl_with_device()
        with pytest.raises(ValueError):
            ctrl.swipe("ABC", 10, 20, 300, 400, "300; id")  # type: ignore[arg-type]
        mock_device.shell.assert_not_called()

    def test_swipe_rejects_non_integer_coordinate_before_shell(self) -> None:
        ctrl, mock_device = self._ctrl_with_device()
        with pytest.raises(ValueError):
            ctrl.swipe("ABC", 10, 20, "300 MARKER", 400, 500)  # type: ignore[arg-type]
        mock_device.shell.assert_not_called()

    def test_long_press_rejects_non_integer_duration_before_shell(self) -> None:
        ctrl, mock_device = self._ctrl_with_device()
        with pytest.raises(ValueError):
            ctrl.long_press("ABC", 100, 200, duration_ms="500; id")  # type: ignore[arg-type]
        mock_device.shell.assert_not_called()

    def test_long_press_rejects_non_integer_coordinate_before_shell(self) -> None:
        ctrl, mock_device = self._ctrl_with_device()
        with pytest.raises(ValueError):
            ctrl.long_press("ABC", "1 MARKER", 200)  # type: ignore[arg-type]
        mock_device.shell.assert_not_called()

    def test_type_text(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.type_text("ABC", "hello")
        mock_device.shell.assert_called_once_with("input text hello", timeout=15)

    def test_type_text_escapes_spaces_as_percent_s(self) -> None:
        """Regression: inbox 9432ecdd. ``adb shell input text "hello world"``
        parses the second word as a stray positional and types only "hello".
        Canonical escape is ``text.replace(' ', '%s')``."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.type_text("ABC", "hello world")

        cmd = mock_device.shell.call_args[0][0]
        # The post-`input text ` payload must be a single shell-token
        # containing the %s escape, never a bare space.
        payload = cmd.split("input text ", 1)[1]
        assert "%s" in payload
        # The literal space-separated `world` must NOT appear as a separate token
        assert " world" not in payload or payload.startswith(("'", '"'))

    def test_type_text_quotes_shell_metacharacters(self) -> None:
        """Regression: inbox 9432ecdd. Text containing shell metas (``;`` ``&``
        ``$`` backticks) must not be interpreted by the device shell as
        command separators or substitutions."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.type_text("ABC", "h;rm -rf /")

        cmd = mock_device.shell.call_args[0][0]
        payload = cmd.split("input text ", 1)[1]
        # Payload must be quoted (single or double quotes) so the device shell
        # treats `;`, `-rf`, `/` as literal characters inside a single arg.
        assert payload.startswith(("'", '"')), (
            f"payload {payload!r} must be quoted to neutralize shell metas"
        )
        # And the space inside must still be escaped to %s for `input text`.
        assert "-rf%s/" in payload

    def test_type_text_handles_embedded_single_quote(self) -> None:
        """Regression: inbox 9432ecdd. Text containing a single quote must
        survive shlex quoting and not raise."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.type_text("ABC", "it's fine")

        cmd = mock_device.shell.call_args[0][0]
        assert cmd.startswith("input text ")
        # %s substitution still happens for the space
        payload = cmd.split("input text ", 1)[1]
        assert "%s" in payload

    def test_clear_app_data_runs_pm_clear(self) -> None:
        """clear_app_data issues ``pm clear <package>`` to the device shell.

        Returns False: the app survives ``pm clear`` install-intact, so no
        reinstall is needed (needs_reinstall contract, 047 Phase B2).
        """
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        result = ctrl.clear_app_data("PIXEL8PRO", "com.sniperpulse.app")

        cmd = mock_device.shell.call_args[0][0]
        assert cmd == "pm clear com.sniperpulse.app"
        assert result is False

    def test_grant_permission_runs_pm_grant(self) -> None:
        """grant_permission issues ``pm grant <package> <permission>``."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.grant_permission("PIXEL8PRO", "com.sniperpulse.app", "android.permission.CAMERA")

        cmd = mock_device.shell.call_args[0][0]
        assert cmd == "pm grant com.sniperpulse.app android.permission.CAMERA"

    def test_press_key(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.press_key("ABC", "HOME")
        mock_device.shell.assert_called_once_with("input keyevent HOME", timeout=15)

    def test_press_key_rejects_shell_metacharacters(self) -> None:
        """Security regression: imp ebcb3be3. ``key`` is interpolated into a
        device-shell command; a metacharacter payload must be rejected before
        anything reaches the shell — never quoted through, never executed."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        for payload in (
            "HOME; rm -rf /sdcard",
            "HOME && reboot",
            "$(reboot)",
            "`reboot`",
            "HOME|id",
            "",
        ):
            with pytest.raises(ValueError):
                ctrl.press_key("ABC", payload)
        mock_device.shell.assert_not_called()

    def test_press_key_allows_all_legit_keycode_forms(self) -> None:
        """`input keyevent` accepts KEYCODE_* names, bare names, and numeric
        codes — the validator must pass all three through unchanged."""
        for key in ("HOME", "KEYCODE_BACK", "66", "VOLUME_UP", "back"):
            ctrl = AndroidController()
            mock_device = MagicMock()
            mock_client = MagicMock()
            mock_client.device.return_value = mock_device
            ctrl._client = mock_client

            ctrl.press_key("ABC", key)
            mock_device.shell.assert_called_once_with(f"input keyevent {key}", timeout=15)


class TestAndroidScrcpyInput:
    """Tests for input routing — scrcpy input is disabled, adb shell is primary.

    The scrcpy control-socket input fast path was removed (touch coordinate
    scaling + stream timeout issues), so input always routes through adb
    shell even when a control stream exists.
    """

    def test_tap_uses_adb_even_when_scrcpy_available(self) -> None:
        """tap() uses adb shell even when a scrcpy control stream exists."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        mock_stream = MagicMock()
        mock_stream.has_control = True
        mock_scrcpy = MagicMock()
        mock_scrcpy.get_stream.return_value = mock_stream
        ctrl._scrcpy = mock_scrcpy
        ctrl._scrcpy_available = True

        ctrl.tap("ABC", 100, 200)
        mock_stream.tap.assert_not_called()
        mock_device.shell.assert_called_once_with("input tap 100 200", timeout=15)

    def test_get_scrcpy_lazy_init_available(self) -> None:
        """First call probes server_available; True → manager created."""
        ctrl = AndroidController()
        assert ctrl._scrcpy_available is None
        with patch("linc_nerve.core.android.server_available", return_value=True):
            scrcpy = ctrl._get_scrcpy()
        assert scrcpy is not None
        assert ctrl._scrcpy_available is True

    def test_get_scrcpy_lazy_init_unavailable(self) -> None:
        """server_available False → None, and the probe is cached."""
        ctrl = AndroidController()
        with patch("linc_nerve.core.android.server_available", return_value=False) as mock_avail:
            assert ctrl._get_scrcpy() is None
            assert ctrl._get_scrcpy() is None  # second call uses cached probe
        mock_avail.assert_called_once()
        assert ctrl._scrcpy_available is False

    def test_get_scrcpy_returns_cached_manager(self) -> None:
        """Once probed available, subsequent calls return the same manager."""
        ctrl = AndroidController()
        sentinel = MagicMock()
        ctrl._scrcpy_available = True
        ctrl._scrcpy = sentinel
        assert ctrl._get_scrcpy() is sentinel

    def test_tap_falls_back_to_adb_when_no_scrcpy(self) -> None:
        """tap() should fall back to adb shell when scrcpy unavailable."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client
        ctrl._scrcpy_available = False
        ctrl._scrcpy = None

        ctrl.tap("ABC", 100, 200)
        mock_device.shell.assert_called_once_with("input tap 100 200", timeout=15)

    def test_tap_falls_back_when_control_not_connected(self) -> None:
        """tap() should fall back if stream exists but has_control is False."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        mock_stream = MagicMock()
        mock_stream.has_control = False
        mock_scrcpy = MagicMock()
        mock_scrcpy.get_stream.return_value = mock_stream
        ctrl._scrcpy = mock_scrcpy
        ctrl._scrcpy_available = True

        ctrl.tap("ABC", 100, 200)
        mock_device.shell.assert_called_once_with("input tap 100 200", timeout=15)

    def test_swipe_uses_adb_even_when_scrcpy_available(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        mock_stream = MagicMock()
        mock_stream.has_control = True
        mock_scrcpy = MagicMock()
        mock_scrcpy.get_stream.return_value = mock_stream
        ctrl._scrcpy = mock_scrcpy
        ctrl._scrcpy_available = True

        ctrl.swipe("ABC", 10, 20, 300, 400, 500)
        mock_stream.swipe.assert_not_called()
        mock_device.shell.assert_called_once_with("input swipe 10 20 300 400 500", timeout=15)

    def test_long_press_uses_adb_even_when_scrcpy_available(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        mock_stream = MagicMock()
        mock_stream.has_control = True
        mock_scrcpy = MagicMock()
        mock_scrcpy.get_stream.return_value = mock_stream
        ctrl._scrcpy = mock_scrcpy
        ctrl._scrcpy_available = True

        ctrl.long_press("ABC", 100, 200, 1000)
        mock_stream.long_press.assert_not_called()
        mock_device.shell.assert_called_once_with("input swipe 100 200 100 200 1000", timeout=15)

    def test_tap_falls_back_on_scrcpy_error(self) -> None:
        """If scrcpy tap raises, fall back to adb shell."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        mock_stream = MagicMock()
        mock_stream.has_control = True
        mock_stream.tap.side_effect = RuntimeError("socket broken")
        mock_scrcpy = MagicMock()
        mock_scrcpy.get_stream.return_value = mock_stream
        ctrl._scrcpy = mock_scrcpy
        ctrl._scrcpy_available = True

        ctrl.tap("ABC", 100, 200)
        # Should have fallen back to adb
        mock_device.shell.assert_called_once_with("input tap 100 200", timeout=15)


class TestAndroidScreenshotPil:
    """Tests for AndroidController.screenshot_pil() scrcpy fast path."""

    def test_returns_image_when_scrcpy_available(self) -> None:
        from PIL import Image

        ctrl = AndroidController()
        mock_stream = MagicMock()
        mock_stream.screenshot_pil.return_value = Image.new("RGB", (100, 200), "green")
        mock_scrcpy = MagicMock()
        mock_scrcpy.is_open.return_value = False
        mock_scrcpy.start_stream.return_value = mock_stream
        ctrl._scrcpy = mock_scrcpy
        ctrl._scrcpy_available = True

        result = ctrl.screenshot_pil("ABC")
        assert isinstance(result, Image.Image)
        assert result.size == (100, 200)

    def test_returns_none_without_scrcpy(self) -> None:
        ctrl = AndroidController()
        ctrl._scrcpy_available = False
        ctrl._scrcpy = None

        result = ctrl.screenshot_pil("ABC")
        assert result is None

    def test_returns_none_when_circuit_open(self) -> None:
        ctrl = AndroidController()
        mock_scrcpy = MagicMock()
        mock_scrcpy.is_open.return_value = True
        ctrl._scrcpy = mock_scrcpy
        ctrl._scrcpy_available = True

        result = ctrl.screenshot_pil("ABC")
        assert result is None


class TestAndroidAppLifecycle:
    def test_install(self) -> None:
        ctrl = AndroidController()
        ctrl._scrcpy_available = False
        cp = MagicMock()
        cp.returncode = 0
        cp.stdout = "Success\n"
        cp.stderr = ""

        with patch("subprocess.run", return_value=cp) as run:
            ctrl.install("ABC", "/path/to/app.apk")

        install_calls = [c for c in run.call_args_list if c.args and "install" in c.args[0]]
        assert install_calls
        assert install_calls[0].args[0] == [
            ADB_PATH,
            "-s",
            "ABC",
            "install",
            "-r",
            "-t",
            "/path/to/app.apk",
        ]

    def test_launch_with_activity(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.launch("ABC", "com.app", ".MainActivity")
        mock_device.shell.assert_called_once_with("am start -n com.app/.MainActivity", timeout=15)

    def test_launch_without_activity_resolved(self) -> None:
        """Launch resolves main activity via dumpsys and uses am start.

        Regression guard (issue #28): on a non-Wear device the phone path is
        unchanged — plain ``resolve-activity --brief`` (no LAUNCHER category) +
        ``am start -n``. The only addition is the upfront Wear-detection probe.
        """
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        # Simulate resolve returning a valid activity
        mock_device.shell.side_effect = [
            # First call: Wear-detection probe — NOT a watch
            "nosdcard,default\n",
            # Second call: resolve-activity
            "priority=0 preferredOrder=0 match=0x108000\ncom.app/.MainActivity\n",
            # Third call: am start
            "Starting: Intent { cmp=com.app/.MainActivity }",
        ]

        ctrl.launch("ABC", "com.app")
        assert mock_device.shell.call_count == 3
        mock_device.shell.assert_any_call(
            "cmd package resolve-activity --brief com.app", timeout=15
        )
        mock_device.shell.assert_any_call("am start -n com.app/.MainActivity", timeout=15)

    def test_launch_without_activity_fallback_monkey(self) -> None:
        """Launch falls back to monkey when activity resolution fails.

        Regression guard (issue #28): the monkey fallback stays available on
        non-Wear devices.
        """
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        # Simulate resolve returning garbage (no match)
        mock_device.shell.side_effect = [
            # First call: Wear-detection probe — NOT a watch
            "nosdcard,default\n",
            # Second call: resolve-activity fails
            "Error: No activity found\n",
            # Third call: monkey fallback
            "Events injected: 1",
        ]

        ctrl.launch("ABC", "com.app")
        assert mock_device.shell.call_count == 3
        mock_device.shell.assert_any_call(
            "monkey -p com.app -c android.intent.category.LAUNCHER 1", timeout=15
        )

    def test_is_wear_os_detects_watch_characteristic(self) -> None:
        """_is_wear_os reads ro.build.characteristics and finds the `watch` token."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        mock_device.shell.return_value = "nosdcard,watch\n"
        assert ctrl._is_wear_os("FAKESERIAL0001") is True
        mock_device.shell.assert_called_once_with("getprop ro.build.characteristics", timeout=15)

    def test_is_wear_os_false_for_phone(self) -> None:
        """_is_wear_os returns False when `watch` is not among the characteristics."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        mock_device.shell.return_value = "nosdcard,default\n"
        assert ctrl._is_wear_os("ABC") is False

    def test_is_wear_os_not_string_sniffing_substring(self) -> None:
        """`watchdog`-style substrings must not be mistaken for the `watch` token."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        mock_device.shell.return_value = "watchband_companion\n"
        assert ctrl._is_wear_os("ABC") is False

    def test_launch_wear_uses_explicit_component_not_monkey(self) -> None:
        """On Wear, package-only launch resolves via LAUNCHER category + am start -n;
        the bare LAUNCHER-monkey fallback is never used (issue #28 AC1+AC2).

        Both the resolve and the start carry ``--include-stopped-packages``:
        a backgrounded/just-installed Wear app sits in FLAG_STOPPED, and Android
        excludes stopped packages from intent resolution — so without the flag
        ``resolve-activity`` returns "No activity found" and ``am start`` reports
        "Activity class does not exist". Verified on Pixel Watch 2 (issue #28).
        """
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        mock_device.shell.side_effect = [
            # 1) Wear-detection probe — IS a watch
            "nosdcard,watch\n",
            # 2) resolve-activity (stopped-inclusive) with explicit LAUNCHER category
            "priority=0 preferredOrder=0 match=0x108000\n"
            "com.lincmobile.level.watch/.WatchActivity\n",
            # 3) am start with explicit component
            "Starting: Intent { cmp=com.lincmobile.level.watch/.WatchActivity }",
        ]

        ctrl.launch("FAKESERIAL0001", "com.lincmobile.level.watch")

        mock_device.shell.assert_any_call(
            "cmd package resolve-activity --brief --include-stopped-packages "
            "-c android.intent.category.LAUNCHER com.lincmobile.level.watch",
            timeout=15,
        )
        mock_device.shell.assert_any_call(
            "am start --include-stopped-packages -n com.lincmobile.level.watch/.WatchActivity",
            timeout=15,
        )
        issued = [c.args[0] for c in mock_device.shell.call_args_list]
        assert not any("monkey" in cmd for cmd in issued), (
            "Wear launch must never fall back to LAUNCHER-monkey"
        )

    def test_launch_wear_refuses_monkey_when_unresolved(self) -> None:
        """On Wear, if the launcher activity can't be resolved, launch() raises
        rather than silently no-op'ing via monkey (issue #28 AC2)."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        mock_device.shell.side_effect = [
            "nosdcard,watch\n",  # Wear-detection probe
            "Error: No activity found\n",  # resolve fails
        ]

        with pytest.raises(RuntimeError, match="resolve"):
            ctrl.launch("FAKESERIAL0001", "com.lincmobile.level.watch")

        issued = [c.args[0] for c in mock_device.shell.call_args_list]
        assert not any("monkey" in cmd for cmd in issued)

    def test_launch_with_uri(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.launch("emulator-5554", "com.example.app", uri="example://settings")
        mock_device.shell.assert_called_once_with(
            "am start -a android.intent.action.VIEW -d example://settings com.example.app",
            timeout=15,
        )

    def test_launch_uri_takes_priority_over_activity(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.launch(
            "emulator-5554", "com.example.app", activity=".MainActivity", uri="example://settings"
        )
        mock_device.shell.assert_called_once_with(
            "am start -a android.intent.action.VIEW -d example://settings com.example.app",
            timeout=15,
        )

    def test_launch_with_uri_ampersand_survives_shell(self) -> None:
        """A `&` in the deep-link URI must not be split apart by the device's `sh`.

        Regression for the unquoted-interpolation shell-injection bug: an
        unquoted URI containing `&` let /bin/sh treat it as a background
        operator, silently dropping the package name and any extras from the
        `am start` invocation that actually reached the device. Fails pre-fix
        (the tokenized tail is truncated at `&`); passes once the URI is
        `shlex.quote`-d.
        """
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.launch(
            "ABC",
            "com.app",
            uri="app://navigate?screen=main&bench=1",
            extras={"SKIP_CONSENT": True},
        )
        cmd = mock_device.shell.call_args.args[0]
        assert _sh_tokenize(cmd) == [
            "am",
            "start",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            "app://navigate?screen=main&bench=1",
            "com.app",
            "--ez",
            "SKIP_CONSENT",
            "true",
        ]

    def test_launch_with_uri_space_and_semicolon_survives_shell(self) -> None:
        """A URI with a space and a `;` must arrive at the device as one literal arg.

        An unquoted space would split the URI into two argv words, and an
        unquoted `;` would terminate the `am start` command and start a new
        one. Fails pre-fix.
        """
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        uri = "app://navigate?screen=two words;rm -rf /tmp"
        ctrl.launch("ABC", "com.app", uri=uri)
        cmd = mock_device.shell.call_args.args[0]
        assert _sh_tokenize(cmd) == [
            "am",
            "start",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            uri,
            "com.app",
        ]

    def test_launch_with_string_extra_containing_spaces_survives_shell(self) -> None:
        """A free-text ``--es`` extra value with spaces must arrive as one literal arg.

        Fails pre-fix: an unquoted ``--es NOTE two words`` splits into three
        argv words at the device shell instead of one key + one value.
        """
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.launch("ABC", "com.app", uri="app://x", extras={"NOTE": "two words"})
        cmd = mock_device.shell.call_args.args[0]
        assert _sh_tokenize(cmd) == [
            "am",
            "start",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            "app://x",
            "com.app",
            "--es",
            "NOTE",
            "two words",
        ]

    # --- intent extras (T6 / reactor 2034b544) ---

    def test_format_extras_type_dispatch(self) -> None:
        """_format_extras emits --ez for bool, --ei for int, --es for str."""
        ctrl = AndroidController()
        assert ctrl._format_extras({"FLAG": True}) == "--ez FLAG true"
        assert ctrl._format_extras({"FLAG": False}) == "--ez FLAG false"
        assert ctrl._format_extras({"COUNT": 3}) == "--ei COUNT 3"
        assert ctrl._format_extras({"NAME": "alice"}) == "--es NAME alice"

    def test_format_extras_bool_before_int(self) -> None:
        """bool must dispatch to --ez, not --ei (Python bool is a subclass of int)."""
        ctrl = AndroidController()
        assert ctrl._format_extras({"SKIP_CONSENT": True}) == "--ez SKIP_CONSENT true"

    def test_format_extras_empty_and_none(self) -> None:
        """None or empty dict produce an empty string (commands stay byte-identical)."""
        ctrl = AndroidController()
        assert ctrl._format_extras(None) == ""
        assert ctrl._format_extras({}) == ""

    def test_format_extras_multiple_preserves_order(self) -> None:
        """Multiple extras are space-joined in insertion order."""
        ctrl = AndroidController()
        out = ctrl._format_extras({"SKIP_CONSENT": True, "SEED": 7, "MODE": "test"})
        assert out == "--ez SKIP_CONSENT true --ei SEED 7 --es MODE test"

    def test_format_extras_rejects_unsupported_type(self) -> None:
        """A non-str/bool/int value is a validation error, not a silent drop."""
        ctrl = AndroidController()
        with pytest.raises(ValueError, match="extras"):
            ctrl._format_extras({"BAD": 3.14})

    @pytest.mark.parametrize(
        "bad_key",
        ["K; id", "K id", "K$(id)", "-leading_dash"],
    )
    def test_format_extras_rejects_invalid_key(self, bad_key: str) -> None:
        """_format_extras raises ValueError mentioning 'extras key' for invalid keys."""
        ctrl = AndroidController()
        with pytest.raises(ValueError, match="extras key"):
            ctrl._format_extras({bad_key: True})

    def test_format_extras_valid_dotted_key(self) -> None:
        """Dotted extras keys like com.linc.EXTRA_SEED format as expected."""
        ctrl = AndroidController()
        assert ctrl._format_extras({"com.linc.EXTRA_SEED": 7}) == "--ei com.linc.EXTRA_SEED 7"

    def test_launch_with_activity_and_extras(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.launch("ABC", "com.app", ".MainActivity", extras={"SKIP_CONSENT": True})
        mock_device.shell.assert_called_once_with(
            "am start -n com.app/.MainActivity --ez SKIP_CONSENT true", timeout=15
        )

    def test_launch_with_uri_and_extras(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.launch(
            "emulator-5554", "com.example.app", uri="example://x", extras={"SKIP_CONSENT": True}
        )
        mock_device.shell.assert_called_once_with(
            "am start -a android.intent.action.VIEW -d example://x com.example.app "
            "--ez SKIP_CONSENT true",
            timeout=15,
        )

    def test_launch_phone_resolved_with_extras(self) -> None:
        """Package-only phone launch threads extras into the resolved am start."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        mock_device.shell.side_effect = [
            "nosdcard,default\n",  # Wear-detection probe — not a watch
            "com.app/.MainActivity\n",  # resolve-activity
            "Starting: Intent { cmp=com.app/.MainActivity }",  # am start
        ]

        ctrl.launch("ABC", "com.app", extras={"SKIP_CONSENT": True})
        mock_device.shell.assert_any_call(
            "am start -n com.app/.MainActivity --ez SKIP_CONSENT true", timeout=15
        )

    def test_launch_wear_with_extras(self) -> None:
        """Wear package-only launch threads extras into the explicit-component am start.

        This is the PrecisionLevel-watch SKIP_CONSENT path (AC3): no manual am-start
        priming needed because the extra rides the normal launch.
        """
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        mock_device.shell.side_effect = [
            "nosdcard,watch\n",  # Wear-detection probe
            "com.lincmobile.level.watch/.WatchActivity\n",  # resolve-activity
            "Starting: Intent { ... }",  # am start
        ]

        ctrl.launch("FAKESERIAL0001", "com.lincmobile.level.watch", extras={"SKIP_CONSENT": True})
        mock_device.shell.assert_any_call(
            "am start --include-stopped-packages -n "
            "com.lincmobile.level.watch/.WatchActivity --ez SKIP_CONSENT true",
            timeout=15,
        )

    def test_launch_phone_monkey_fallback_with_extras_raises(self) -> None:
        """Extras can't ride the LAUNCHER-monkey fallback → raise, don't silently drop."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        mock_device.shell.side_effect = [
            "nosdcard,default\n",  # not a watch
            "Error: No activity found\n",  # resolve fails → would fall to monkey
        ]

        with pytest.raises(RuntimeError, match="extras"):
            ctrl.launch("ABC", "com.app", extras={"SKIP_CONSENT": True})

        issued = [c.args[0] for c in mock_device.shell.call_args_list]
        assert not any("monkey" in cmd for cmd in issued)

    def test_launch_no_extras_unchanged(self) -> None:
        """Regression: omitting extras leaves the activity command byte-identical."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.launch("ABC", "com.app", ".MainActivity")
        mock_device.shell.assert_called_once_with("am start -n com.app/.MainActivity", timeout=15)

    def test_kill(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.kill("ABC", "com.app")
        mock_device.shell.assert_called_once_with("am force-stop com.app", timeout=15)

    def test_launch_rejects_shell_metacharacters_in_activity(self) -> None:
        """Hostile activity name raises ValueError and device.shell is never called."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        with pytest.raises(ValueError, match="activity"):
            ctrl.launch("ABC", "com.app", activity="com.app.Main; id")
        mock_device.shell.assert_not_called()

    def test_launch_rejects_shell_metacharacters_in_package(self) -> None:
        """Hostile package name in launch raises ValueError and device.shell is never called."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        with pytest.raises(ValueError, match="package"):
            ctrl.launch("ABC", "com.app; id")
        mock_device.shell.assert_not_called()

    def test_kill_rejects_shell_metacharacters_in_package(self) -> None:
        """Hostile package name in kill raises ValueError and device.shell is never called."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        with pytest.raises(ValueError, match="package"):
            ctrl.kill("ABC", "com.app > /sdcard/x")
        mock_device.shell.assert_not_called()

    def test_launch_valid_activity_pin(self) -> None:
        """Legal activity names produce exact am start -n command."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.launch("ABC", "com.app", activity=".MainActivity$Inner")
        # Quoted so `$Inner` reaches `am` literally (security review 2026-10-02 L1/H1).
        mock_device.shell.assert_called_once_with(
            "am start -n 'com.app/.MainActivity$Inner'", timeout=15
        )

        mock_device.shell.reset_mock()
        ctrl.launch("ABC", "com.app", activity="com.app.ui.MainActivity")
        mock_device.shell.assert_called_once_with(
            "am start -n com.app/com.app.ui.MainActivity", timeout=15
        )

    def test_launch_empty_package_with_uri_pin(self) -> None:
        """Empty package with uri produces quoted am start -a VIEW command."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.launch("ABC", "", uri="app://x?a=1&b=2")
        mock_device.shell.assert_called_once_with(
            "am start -a android.intent.action.VIEW -d 'app://x?a=1&b=2' ", timeout=15
        )

    def test_list_apps(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = "package:com.app.one\npackage:com.app.two\n"
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        apps = ctrl.list_apps("ABC")
        assert apps == ["com.app.one", "com.app.two"]

    def test_uninstall(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.uninstall("ABC", "com.app")
        mock_device.shell.assert_called_once_with("pm uninstall com.app", timeout=15)

    def test_parse_resolved_activity_exact_match(self) -> None:
        # Prefix-sharing package should return None
        res1 = AndroidController._parse_resolved_activity("com.app.extra/.Main", "com.app")
        assert res1 is None

        # Exact package should return the activity
        res2 = AndroidController._parse_resolved_activity("com.app/.MainActivity", "com.app")
        assert res2 == ".MainActivity"


class TestAndroidAppLifecycleInjectionValidation:
    """Issue #94 gap 2: uninstall/clear_app_data/grant_permission interpolate
    caller-supplied package/permission strings directly into an ``adb shell``
    command with no validation. These tests pin the accident-guard: shell
    metacharacters and leading-dash flag injection must never reach the shell.
    """

    SHELL_METACHAR_PAYLOADS: ClassVar[list[str]] = [
        "com.x; rm -rf /tmp/x",
        "com.x`touch /tmp/pwned`",
    ]

    def _controller(self) -> tuple[AndroidController, MagicMock]:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client
        return ctrl, mock_device

    @pytest.mark.parametrize("payload", SHELL_METACHAR_PAYLOADS)
    def test_uninstall_rejects_shell_metachar_package(self, payload: str) -> None:
        ctrl, mock_device = self._controller()
        with pytest.raises(ValueError):
            ctrl.uninstall("ABC", payload)
        mock_device.shell.assert_not_called()

    @pytest.mark.parametrize("payload", SHELL_METACHAR_PAYLOADS)
    def test_clear_app_data_rejects_shell_metachar_package(self, payload: str) -> None:
        ctrl, mock_device = self._controller()
        with pytest.raises(ValueError):
            ctrl.clear_app_data("ABC", payload)
        mock_device.shell.assert_not_called()

    @pytest.mark.parametrize("payload", SHELL_METACHAR_PAYLOADS)
    def test_grant_permission_rejects_shell_metachar_package(self, payload: str) -> None:
        ctrl, mock_device = self._controller()
        with pytest.raises(ValueError):
            ctrl.grant_permission("ABC", payload, "android.permission.CAMERA")
        mock_device.shell.assert_not_called()

    def test_uninstall_rejects_leading_dash_flag_injection(self) -> None:
        ctrl, mock_device = self._controller()
        with pytest.raises(ValueError):
            ctrl.uninstall("ABC", "-n com.evil.package")
        mock_device.shell.assert_not_called()

    def test_clear_app_data_rejects_leading_dash_flag_injection(self) -> None:
        ctrl, mock_device = self._controller()
        with pytest.raises(ValueError):
            ctrl.clear_app_data("ABC", "-n com.evil.package")
        mock_device.shell.assert_not_called()

    def test_grant_permission_rejects_leading_dash_flag_injection(self) -> None:
        ctrl, mock_device = self._controller()
        with pytest.raises(ValueError):
            ctrl.grant_permission("ABC", "-n com.evil.package", "android.permission.CAMERA")
        mock_device.shell.assert_not_called()

    def test_grant_permission_validates_permission_argument_too(self) -> None:
        """Validation must cover the second interpolated field, not just package."""
        ctrl, mock_device = self._controller()
        with pytest.raises(ValueError):
            ctrl.grant_permission(
                "ABC", "com.lincmobile.level", "android.permission.CAMERA; rm -rf /tmp/x"
            )
        mock_device.shell.assert_not_called()

    def test_uninstall_accepts_well_formed_package(self) -> None:
        """A valid literal still works after validation is added (pinned command)."""
        ctrl, mock_device = self._controller()
        ctrl.uninstall("ABC", "com.lincmobile.level")
        mock_device.shell.assert_called_once_with("pm uninstall com.lincmobile.level", timeout=15)


class TestAndroidHierarchy:
    def test_elements_basic(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<hierarchy rotation="0">'
            '<node resource-id="com.app:id/btn" text="OK" '
            'class="android.widget.Button" bounds="[0,0][100,50]" '
            'clickable="true" enabled="true" checked="false" focused="false" />'
            "</hierarchy>"
            "UI hierachy dumped to: /dev/tty"
        )
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        elements = ctrl.elements("ABC")
        assert len(elements) == 1
        assert elements[0]["id"] == "com.app:id/btn"
        assert elements[0]["text"] == "OK"
        assert elements[0]["clickable"] is True

    def test_elements_filters_empty_nodes(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = (
            '<hierarchy rotation="0">'
            '<node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" '
            'clickable="false" enabled="true" resource-id="" text="" content-desc="" />'
            "</hierarchy>"
        )
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        elements = ctrl.elements("ABC")
        assert len(elements) == 0  # Empty node filtered out

    def test_elements_non_xml_output_raises_not_empty(self) -> None:
        """Reactor 8bba730a: a failed dump must NEVER read as an empty screen."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = "not xml at all"
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        with pytest.raises(UiHierarchyUnavailableError) as exc_info:
            ctrl.elements("ABC")
        assert "UiAutomation" in str(exc_info.value)

    _VALID_HIERARCHY = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<hierarchy rotation="0">'
        '<node resource-id="com.app:id/btn" text="OK" '
        'class="android.widget.Button" bounds="[0,0][100,50]" '
        'clickable="true" enabled="true" checked="false" focused="false" />'
        "</hierarchy>"
    )

    def test_elements_killed_dump_recovers_by_stopping_leaked_holder(self) -> None:
        """Reactor 280bbee7: `uiautomator dump` is SIGKILLed ("Killed") while a
        leaked automation driver (dev.mobile.maestro left by a prior grid run)
        holds the device's single UiAutomation connection. nerve force-stops
        the known holder and the retry dump succeeds."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        calls: list[str] = []

        def shell_router(cmd: str, **kw: object) -> str:
            calls.append(cmd)
            if cmd == "uiautomator dump /dev/tty":
                recovered = any(c.startswith("am force-stop") for c in calls[:-1])
                return self._VALID_HIERARCHY if recovered else "Killed"
            if cmd.startswith("ps"):
                return (
                    "com.android.shell\n"
                    "dev.mobile.maestro\n"
                    "com.google.android.apps.wearables.maestro.companion\n"
                )
            return ""

        mock_device.shell.side_effect = shell_router
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        elements = ctrl.elements("ABC")
        assert len(elements) == 1
        assert elements[0]["text"] == "OK"
        force_stops = [c for c in calls if c.startswith("am force-stop")]
        # Only the allowlisted driver is stopped — never the (unrelated)
        # Pixel Watch companion app whose package also contains "maestro".
        assert force_stops == ["am force-stop dev.mobile.maestro"]

    def test_elements_killed_dump_without_known_holder_raises(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        calls: list[str] = []

        def shell_router(cmd: str, **kw: object) -> str:
            calls.append(cmd)
            if cmd == "uiautomator dump /dev/tty":
                return "Killed"
            if cmd.startswith("ps"):
                return "com.android.shell\ncom.android.systemui\n"
            return ""

        mock_device.shell.side_effect = shell_router
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        with pytest.raises(UiHierarchyUnavailableError) as exc_info:
            ctrl.elements("ABC")
        assert "Killed" in str(exc_info.value)
        assert not any(c.startswith("am force-stop") for c in calls)

    def test_elements_killed_dump_still_broken_after_recovery_raises(self) -> None:
        """Holder found and stopped but the dump stays dead → loud error
        telling the caller to reboot, still never an empty list."""
        ctrl = AndroidController()
        mock_device = MagicMock()

        def shell_router(cmd: str, **kw: object) -> str:
            if cmd == "uiautomator dump /dev/tty":
                return "Killed"
            if cmd.startswith("ps"):
                return "dev.mobile.maestro\n"
            return ""

        mock_device.shell.side_effect = shell_router
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        with pytest.raises(UiHierarchyUnavailableError) as exc_info:
            ctrl.elements("ABC")
        assert "reboot" in str(exc_info.value)

    def test_elements_hang_dump_recovers_by_stopping_leaked_holders(self) -> None:
        """Reactor 9b9ba809 / #119 findings 1+2: a leaked UiAutomation
        holder can also cause a silent HANG (a timeout exception, no
        output at all) instead of the "Killed" rc-137 case above. Two-tier
        heal: tier 1 (orphan cleanup + plain retry, no force-stop) also
        fails here, so tier 2 (evidence-gated force-stop of the known
        holder allowlist) fires and its retry succeeds — three total dump
        attempts.

        ``_shell`` is mocked directly (not ``adbutils`` device.shell) so the
        internal retry/backoff inside ``_shell`` itself is not exercised —
        matching the issue AC's "with `_shell` mocked" wording and the
        existing pattern in tests/test_ensure_ready.py."""
        ctrl = AndroidController()
        calls: list[str] = []

        def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
            calls.append(cmd)
            if cmd == "uiautomator dump /dev/tty":
                dump_calls = [c for c in calls if c == "uiautomator dump /dev/tty"]
                if len(dump_calls) < 3:
                    raise TimeoutError("adb read timeout")
                return self._VALID_HIERARCHY
            if cmd.startswith("ps"):
                return "com.android.shell\ndev.mobile.maestro\ncom.github.uiautomator\n"
            return ""

        ctrl._shell = MagicMock(side_effect=fake_shell)

        with structlog.testing.capture_logs() as cap_logs:
            elements = ctrl.elements("ABC")
        assert len(elements) == 1
        assert elements[0]["text"] == "OK"
        force_stops = {c for c in calls if c.startswith("am force-stop")}
        assert force_stops == {
            "am force-stop dev.mobile.maestro",
            "am force-stop com.github.uiautomator",
        }
        # Neither driver package's argv contains the orphan-dump token, so
        # the tier-1/tier-2 orphan cleanup must never fire a kill here.
        assert not any(c.startswith("kill ") for c in calls)
        # AC4's named success signal (#119 finding 5): the recovery log
        # must actually be emitted, not just structurally reachable.
        recovered = [e for e in cap_logs if e.get("event") == "uiautomator_dump_recovered"]
        assert len(recovered) == 1
        assert recovered[0]["holders"] == ["dev.mobile.maestro", "com.github.uiautomator"]
        assert recovered[0]["orphan_killed"] is False

    def test_elements_hang_dump_recovers_without_force_stop_when_retry_succeeds(
        self,
    ) -> None:
        """#119 finding 2: a bare single timeout is not, by itself, evidence
        of a leaked holder — it could be a merely slow device, or a driver
        a concurrent caller legitimately owns (e.g. a Maestro flow nightly
        BVT runs on this same shared bench). If the tier-1 plain retry (no
        force-stop) succeeds, that concurrent caller's driver must be left
        completely untouched."""
        ctrl = AndroidController()
        calls: list[str] = []

        def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
            calls.append(cmd)
            if cmd == "uiautomator dump /dev/tty":
                dump_calls = [c for c in calls if c == "uiautomator dump /dev/tty"]
                if len(dump_calls) == 1:
                    raise TimeoutError("adb read timeout")
                return self._VALID_HIERARCHY
            if cmd.startswith("ps"):
                # dev.mobile.maestro is a CONCURRENT caller's legitimate,
                # currently-running flow — it must survive this recovery.
                return "com.android.shell\ndev.mobile.maestro\n"
            return ""

        ctrl._shell = MagicMock(side_effect=fake_shell)

        with structlog.testing.capture_logs() as cap_logs:
            elements = ctrl.elements("ABC")
        assert len(elements) == 1
        assert not any(c.startswith("am force-stop") for c in calls)
        # AC4's named success signal (#119 finding 5): tier-1 (no
        # force-stop) recovery must log too, with an empty holders list.
        recovered = [e for e in cap_logs if e.get("event") == "uiautomator_dump_recovered"]
        assert len(recovered) == 1
        assert recovered[0]["holders"] == []
        assert recovered[0]["orphan_killed"] is False

    def test_elements_hang_dump_kills_orphaned_process_before_retry(self) -> None:
        """#119 finding 1 (round 1): matched by realistic ARGV, not `comm`.
        On a real device `/system/bin/uiautomator` execs `app_process`, so
        `ps -A -o NAME` reports the dump process's comm as `app_process64`,
        never the literal string `uiautomator` — a comm-based match (the
        round-0 implementation) would never fire here. This stub returns
        the realistic `PID ARGS` shape (`app_process64 ...
        com.android.commands.uiautomator.Uiautomator dump /dev/tty`);
        assert the orphan-cleanup shell commands (a ps check + a targeted
        kill-by-pid — never a package, never the driver allowlist) are
        issued strictly BETWEEN the two dump calls."""
        ctrl = AndroidController()
        calls: list[str] = []

        def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
            calls.append(cmd)
            if cmd == "uiautomator dump /dev/tty":
                dump_calls = [c for c in calls if c == "uiautomator dump /dev/tty"]
                if len(dump_calls) == 1:
                    raise TimeoutError("adb read timeout")
                return self._VALID_HIERARCHY
            if cmd.startswith("ps"):
                # Realistic `ps -A -o PID,ARGS` output: the dump process's
                # comm is app_process64, never "uiautomator" — only its
                # argv names the uiautomator command class.
                return (
                    "  123 com.android.shell\n"
                    "  456 app_process64 /system/bin --nice-name=uiautomator "
                    "com.android.commands.uiautomator.Uiautomator dump /dev/tty\n"
                )
            return ""

        ctrl._shell = MagicMock(side_effect=fake_shell)

        elements = ctrl.elements("ABC")
        assert len(elements) == 1

        first_dump_idx = calls.index("uiautomator dump /dev/tty")
        second_dump_idx = calls.index("uiautomator dump /dev/tty", first_dump_idx + 1)
        between = calls[first_dump_idx + 1 : second_dump_idx]
        assert "ps -A -o PID,ARGS" in between
        assert "kill 456" in between
        # Cleanup targeted the dump process by pid only — never the
        # driver allowlist (no evidence of a persistent conflict yet).
        assert not any(c.startswith("am force-stop") for c in calls)

    def test_elements_hang_dump_orphan_kill_ignores_unrelated_processes(self) -> None:
        """#119 finding 1: a `ps` snapshot with no orphaned dump process
        (comm-only entries, no `com.android.commands.uiautomator` argv
        token) must never produce a spurious kill — including against a
        driver allowlist package that merely happens to be running."""
        ctrl = AndroidController()
        calls: list[str] = []

        def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
            calls.append(cmd)
            if cmd == "uiautomator dump /dev/tty":
                dump_calls = [c for c in calls if c == "uiautomator dump /dev/tty"]
                if len(dump_calls) == 1:
                    raise TimeoutError("adb read timeout")
                return self._VALID_HIERARCHY
            if cmd.startswith("ps"):
                return "  123 com.android.shell\n  789 dev.mobile.maestro\n"
            return ""

        ctrl._shell = MagicMock(side_effect=fake_shell)

        elements = ctrl.elements("ABC")
        assert len(elements) == 1
        assert not any(c.startswith("kill ") for c in calls)
        assert not any(c.startswith("am force-stop") for c in calls)

    def test_elements_hang_dump_recovery_deadline_exhausted_skips_tier2(self) -> None:
        """#119 finding 2 (round 1, tightened round 2 / CEO ITEM B): once the
        recovery time budget is spent, tier 2 (a second orphan sweep + the
        full holder allowlist + a third dump) must NOT run — escalate
        immediately with the reboot message instead of chaining further
        shell calls past the caller's tool ceiling. Forces the
        budget-exhausted branch deterministically by patching the deadline
        constant to 0 — round 2 also added a deadline check immediately
        before the tier-1 retry dump itself (not just between tiers), so
        with a zero budget that retry is skipped too: only the original
        failing dump attempt fires."""
        ctrl = AndroidController()
        calls: list[str] = []

        def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
            calls.append(cmd)
            if cmd == "uiautomator dump /dev/tty":
                raise TimeoutError("adb read timeout")
            if cmd.startswith("ps"):
                return "com.android.shell\n"
            return ""

        ctrl._shell = MagicMock(side_effect=fake_shell)

        # Imported locally: android.py imports `_UiMixin` from android_ui,
        # so a module-level `from linc_nerve.core import android_ui` would
        # sort (isort) before the `android` import above and reproduce a
        # circular-import ImportError at collection time.
        from linc_nerve.core import android_ui

        with (
            patch.object(android_ui, "_RECOVERY_DEADLINE_S", 0.0),
            pytest.raises(UiHierarchyUnavailableError) as exc_info,
        ):
            ctrl.elements("ABC")
        msg = str(exc_info.value)
        assert "reboot" in msg
        assert "recovery time budget" in msg
        # Tier 2 never ran, and — round 2 — neither did the tier-1 retry
        # dump: exactly one dump attempt total (the original failing dump),
        # no force-stop calls, and no tier-2 orphan-cleanup ps sweep.
        dump_calls = [c for c in calls if c == "uiautomator dump /dev/tty"]
        assert len(dump_calls) == 1
        assert not any(c.startswith("am force-stop") for c in calls)

    def test_elements_hang_dump_still_hangs_after_recovery_raises_reboot_guidance(
        self,
    ) -> None:
        """Reactor 9b9ba809: holders were found and force-stopped but the
        retry dump ALSO times out — escalate loudly with the literal words
        the nightly pipeline and humans pattern-match on."""
        ctrl = AndroidController()

        def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
            if cmd == "uiautomator dump /dev/tty":
                raise TimeoutError("adb read timeout")
            if cmd.startswith("ps"):
                return "dev.mobile.maestro\n"
            return ""

        ctrl._shell = MagicMock(side_effect=fake_shell)

        with pytest.raises(UiHierarchyUnavailableError) as exc_info:
            ctrl.elements("ABC")
        msg = str(exc_info.value)
        assert "timed out" in msg
        assert "force-stopp" in msg
        assert "reboot" in msg

    def test_elements_hang_dump_without_known_holder_raises_reboot_guidance(self) -> None:
        """Bench-device escalation is unconditional for the hang class too —
        even with no known holder to force-stop, the error must still say
        reboot (matching the existing non-XML branch's unconditional
        escalation)."""
        ctrl = AndroidController()

        def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
            if cmd == "uiautomator dump /dev/tty":
                raise TimeoutError("adb read timeout")
            if cmd.startswith("ps"):
                return "com.android.shell\ncom.android.systemui\n"
            return ""

        ctrl._shell = MagicMock(side_effect=fake_shell)

        with pytest.raises(UiHierarchyUnavailableError) as exc_info:
            ctrl.elements("ABC")
        assert "reboot" in str(exc_info.value)

    def test_elements_hang_dump_retry_returns_non_xml_then_reboot_guidance(self) -> None:
        """#119 finding 4: the tier-1 retry can itself come back with
        non-XML output (e.g. "Killed") rather than raising — an untested
        branch pre-fix, and per finding 1 plausibly the MOST common real
        outcome (the second client also collides with the still-wedged
        connection). Must still converge on the same 'timed out ... then
        failed on retry' + reboot escalation once tier 2's own retry also
        fails to recover it."""
        ctrl = AndroidController()
        calls: list[str] = []

        def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
            calls.append(cmd)
            if cmd == "uiautomator dump /dev/tty":
                dump_calls = [c for c in calls if c == "uiautomator dump /dev/tty"]
                if len(dump_calls) == 1:
                    raise TimeoutError("adb read timeout")
                return "Killed"
            if cmd.startswith("ps"):
                return "dev.mobile.maestro\n"
            return ""

        ctrl._shell = MagicMock(side_effect=fake_shell)

        with pytest.raises(UiHierarchyUnavailableError) as exc_info:
            ctrl.elements("ABC")
        msg = str(exc_info.value)
        assert "timed out" in msg
        assert "retry" in msg
        assert "Killed" in msg
        assert "reboot" in msg

    def test_elements_hang_dump_retry_returns_empty_then_reboot_guidance(self) -> None:
        """Same untested branch (#119 finding 4) but the non-XML retry
        output is empty — must fall back to the '<empty output>' detail
        rather than crashing on an out-of-range index."""
        ctrl = AndroidController()
        calls: list[str] = []

        def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
            calls.append(cmd)
            if cmd == "uiautomator dump /dev/tty":
                dump_calls = [c for c in calls if c == "uiautomator dump /dev/tty"]
                if len(dump_calls) == 1:
                    raise TimeoutError("adb read timeout")
                return ""
            if cmd.startswith("ps"):
                return "dev.mobile.maestro\n"
            return ""

        ctrl._shell = MagicMock(side_effect=fake_shell)

        with pytest.raises(UiHierarchyUnavailableError) as exc_info:
            ctrl.elements("ABC")
        msg = str(exc_info.value)
        assert "timed out" in msg
        assert "retry" in msg
        assert "<empty output>" in msg
        assert "reboot" in msg

    def test_elements_hang_dump_survives_circuit_open_during_recovery(self) -> None:
        """CEO ITEM A (BLOCKING, round 2). Every other hang-recovery test in
        this class patches ``ctrl._shell`` directly — replacing the very
        method that owns ``_check_circuit`` — so none of them can reach the
        circuit-open path; the round-2 review called this out as vacuous
        proof. This test mocks ONE LAYER BELOW ``_shell`` instead (the
        adbutils device's ``.shell``, the same seam ``TestCircuitBreaker``
        uses below), so ``_shell``'s own retry loop and real circuit
        breaker run for real.

        A real wedge routinely trips the per-device circuit breaker
        (``_CIRCUIT_FAILURE_THRESHOLD = 3``) DURING dump #1's own 3 internal
        ``_shell`` retries — so by the time ``_recover_from_dump_hang``'s
        recovery helpers run, the circuit is already OPEN and every
        recovery shell call raises ``DeviceError`` (circuit-open), not a
        plain retryable error. Before the fix that ``DeviceError``
        propagated UNCAUGHT out of ``_kill_orphaned_dump_process``. After
        the fix, recovery treats it like any other failed aux/retry call
        and still converges on ``UiHierarchyUnavailableError`` mentioning
        'reboot' — never a bare ``DeviceError`` (which
        ``pytest.raises(UiHierarchyUnavailableError)`` below would fail to
        catch, since ``DeviceError`` is ``UiHierarchyUnavailableError``'s
        PARENT class, not a subclass)."""
        from linc_nerve.core.android import CircuitState

        mock_device = MagicMock(shell=MagicMock(side_effect=TimeoutError("device wedged")))
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl = _make_controller()
        ctrl._client = mock_client
        ctrl._get_client = lambda: mock_client  # type: ignore[assignment]

        with pytest.raises(UiHierarchyUnavailableError) as exc_info:
            ctrl.elements("ABC")
        assert "reboot" in str(exc_info.value)
        # Proof the circuit genuinely opened as a side effect of dump #1's
        # own retries — the recovery path above ran under a REAL open
        # breaker, not a bypassed one.
        assert ctrl._circuit_breakers["ABC"].state == CircuitState.OPEN

    def test_elements_hang_dump_budget_exhausted_mid_tier2_skips_final_retry(self) -> None:
        """CEO ITEM B (round 2): the deadline must be re-checked immediately
        before the tier-2 retry dump itself, not only between tier 1 and
        tier 2 — a wedge whose tier-2 orphan-sweep + holder force-stop
        alone eats the remaining budget must escalate immediately rather
        than still fire the final retry dump with a full, unscaled
        timeout. Fakes ``time.monotonic()`` to jump past the deadline
        specifically during tier-2's holder-check ``ps`` call, so the
        budget is still available when tier 2 STARTS but is gone by the
        time the final retry dump would fire — the exact "budget spent
        mid-tier" gap the round-2 review named."""
        from linc_nerve.core import android_ui

        ctrl = AndroidController()
        calls: list[str] = []
        clock = [0.0]

        def fake_shell(serial: str, cmd: str, timeout: int = 15) -> str:
            calls.append(cmd)
            if cmd == "uiautomator dump /dev/tty":
                raise TimeoutError("adb read timeout")
            if cmd == "ps -A -o NAME":
                # Tier-2's holder check: jump the clock past the deadline
                # mid-tier, so the checkpoint immediately before the final
                # retry dump sees the budget already spent.
                clock[0] = 1000.0
                return "com.android.shell\n"
            if cmd.startswith("ps"):
                return "com.android.shell\n"
            return ""

        ctrl._shell = MagicMock(side_effect=fake_shell)

        with (
            patch.object(android_ui._android.time, "monotonic", side_effect=lambda: clock[0]),
            pytest.raises(UiHierarchyUnavailableError) as exc_info,
        ):
            ctrl.elements("ABC")
        msg = str(exc_info.value)
        assert "reboot" in msg
        assert "recovery time budget" in msg
        assert "tier-2" in msg
        # Exactly two dump attempts fired (the original hang + the tier-1
        # retry); the tier-2 final retry must NOT have fired.
        dump_calls = [c for c in calls if c == "uiautomator dump /dev/tty"]
        assert len(dump_calls) == 2
        assert not any(c.startswith("am force-stop") for c in calls)

    _MALFORMED_TERMINATED_HIERARCHY = (
        '<hierarchy rotation="0"><node text="OK"></badtag></hierarchy>'
    )

    def test_elements_parse_error_retries_and_recovers(self) -> None:
        """A dump can end with `</hierarchy>` (so `_dump_hierarchy` returns it)
        yet still fail `ET.fromstring` — observed 12x in 73s on the Pixel
        Watch 2 under load (2026-07-14, role: watch bench). A transient
        malformed dump followed by a clean one must return the parsed
        elements instead of raising."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        calls: list[str] = []

        def shell_router(cmd: str, **kw: object) -> str:
            calls.append(cmd)
            if cmd == "uiautomator dump /dev/tty":
                dump_calls = [c for c in calls if c == "uiautomator dump /dev/tty"]
                if len(dump_calls) == 1:
                    return self._MALFORMED_TERMINATED_HIERARCHY
                return self._VALID_HIERARCHY
            return ""

        mock_device.shell.side_effect = shell_router
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        elements = ctrl.elements("ABC")
        assert len(elements) == 1
        assert elements[0]["text"] == "OK"

    def test_elements_parse_error_persistent_raises_with_excerpt(self) -> None:
        """Never returns [] on failure (reactor 8bba730a); the raw
        unparseable output excerpt must be diagnosable from the exception
        message alone, since that's what lands in the daemon log."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = self._MALFORMED_TERMINATED_HIERARCHY
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        with pytest.raises(UiHierarchyUnavailableError) as exc_info:
            ctrl.elements("ABC")
        assert "badtag" in str(exc_info.value)

    def test_elements_parse_error_persistent_makes_exactly_three_dump_calls(self) -> None:
        """Initial attempt + 2 retries = 3 total dump invocations before
        giving up, never silently returning an empty list."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        calls: list[str] = []

        def shell_router(cmd: str, **kw: object) -> str:
            calls.append(cmd)
            if cmd == "uiautomator dump /dev/tty":
                return self._MALFORMED_TERMINATED_HIERARCHY
            return ""

        mock_device.shell.side_effect = shell_router
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        with pytest.raises(UiHierarchyUnavailableError):
            ctrl.elements("ABC")

        dump_calls = [c for c in calls if c == "uiautomator dump /dev/tty"]
        assert len(dump_calls) == 3, f"expected 3 dump calls, got {len(dump_calls)}"

    def test_elements_caches_within_ttl(self) -> None:
        """Regression: inbox 23f41765. `uiautomator dump` p50 is 3.2s on a
        populated Pixel; consecutive callers (state→elements→state) pay the
        full cost each call. A short per-(serial, max_depth) TTL cache
        collapses repeats."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = (
            '<hierarchy rotation="0">'
            '<node resource-id="com.app:id/btn" text="OK" '
            'class="android.widget.Button" bounds="[0,0][100,50]" '
            'clickable="true" enabled="true" checked="false" focused="false" />'
            "</hierarchy>"
        )
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.elements("ABC")
        ctrl.elements("ABC")
        ctrl.elements("ABC")

        # uiautomator dump shelled out only once across 3 calls
        dump_calls = [c for c in mock_device.shell.call_args_list if "uiautomator dump" in str(c)]
        assert len(dump_calls) == 1, f"expected 1 dump call, got {len(dump_calls)}"

    def test_elements_cache_expires_after_ttl(self) -> None:
        """After TTL elapses, `uiautomator dump` runs again on the next call."""
        from linc_nerve.core import android as android_mod

        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = (
            '<hierarchy rotation="0">'
            '<node resource-id="x" text="y" class="c" bounds="[0,0][1,1]" '
            'clickable="true" enabled="true" checked="false" focused="false" />'
            "</hierarchy>"
        )
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.elements("ABC")
        # Backdate cache to simulate TTL expiry (key uses the default max_depth)
        (cache_key,) = ctrl._elements_cache.keys()
        ts, body = ctrl._elements_cache[cache_key]
        ctrl._elements_cache[cache_key] = (
            ts - android_mod._ELEMENTS_CACHE_TTL - 1,
            body,
        )
        ctrl.elements("ABC")

        dump_calls = [c for c in mock_device.shell.call_args_list if "uiautomator dump" in str(c)]
        assert len(dump_calls) == 2

    def test_elements_cache_keyed_by_serial_and_depth(self) -> None:
        """Cache is keyed by (serial, max_depth) — calls with different
        max_depth share no cache entry."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = (
            '<hierarchy rotation="0"><node resource-id="x" text="y" '
            'class="c" bounds="[0,0][1,1]" clickable="true" enabled="true" '
            'checked="false" focused="false" /></hierarchy>'
        )
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.elements("ABC", max_depth=10)
        ctrl.elements("ABC", max_depth=20)  # different depth → distinct cache key
        ctrl.elements("DEF", max_depth=10)  # different serial → distinct cache key

        dump_calls = [c for c in mock_device.shell.call_args_list if "uiautomator dump" in str(c)]
        assert len(dump_calls) == 3

    def test_elements_force_refresh_bypasses_cache(self) -> None:
        """`force_refresh=True` re-dumps even if a cached entry is fresh."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = (
            '<hierarchy rotation="0"><node resource-id="x" text="y" '
            'class="c" bounds="[0,0][1,1]" clickable="true" enabled="true" '
            'checked="false" focused="false" /></hierarchy>'
        )
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.elements("ABC")
        ctrl.elements("ABC", force_refresh=True)

        dump_calls = [c for c in mock_device.shell.call_args_list if "uiautomator dump" in str(c)]
        assert len(dump_calls) == 2

    def test_tap_invalidates_elements_cache(self) -> None:
        """Any UI-mutating call (tap/swipe/long_press/type/key) must
        invalidate the cache — otherwise post-interaction `elements()`
        returns the pre-interaction snapshot."""
        ctrl = AndroidController()
        ctrl._scrcpy_available = False
        mock_device = MagicMock()
        mock_device.shell.return_value = (
            '<hierarchy rotation="0"><node resource-id="x" text="y" '
            'class="c" bounds="[0,0][1,1]" clickable="true" enabled="true" '
            'checked="false" focused="false" /></hierarchy>'
        )
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.elements("ABC")
        ctrl.tap("ABC", 100, 200)
        ctrl.elements("ABC")  # cache should be invalidated by tap

        dump_calls = [c for c in mock_device.shell.call_args_list if "uiautomator dump" in str(c)]
        assert len(dump_calls) == 2

    def test_elements_scrollable_parsed(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = (
            '<hierarchy rotation="0">'
            '<node resource-id="com.app:id/list" text="" '
            'class="android.widget.ListView" bounds="[0,100][1080,2000]" '
            'clickable="false" enabled="true" checked="false" focused="false" '
            'scrollable="true" content-desc="" />'
            "</hierarchy>"
        )
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        elements = ctrl.elements("ABC")
        assert len(elements) == 1
        assert elements[0]["scrollable"] is True
        assert elements[0]["id"] == "com.app:id/list"

    def test_elements_scrollable_no_id_included(self) -> None:
        """Scrollable containers without text/id should still be included."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = (
            '<hierarchy rotation="0">'
            '<node resource-id="" text="" '
            'class="android.widget.ScrollView" bounds="[0,0][1080,2400]" '
            'clickable="false" enabled="true" checked="false" focused="false" '
            'scrollable="true" content-desc="" />'
            "</hierarchy>"
        )
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        elements = ctrl.elements("ABC")
        assert len(elements) == 1
        assert elements[0]["scrollable"] is True


class TestAndroidDeviceInfo:
    def test_get_foreground(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = (
            "  mResumedActivity: ActivityRecord{abc u0 com.lincmobile.level/.MainActivity t45}"
        )
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        fg = ctrl.get_foreground("ABC")
        assert fg["package"] == "com.lincmobile.level"
        assert fg["activity"] == ".MainActivity"

    def test_get_screen_size(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = "Physical size: 1080x2400"
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        size = ctrl.get_screen_size("ABC")
        assert size == {"width": 1080, "height": 2400}

    def test_get_density_parses_wm_density(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = "Physical density: 420\n"
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        assert ctrl.get_density("ABC") == {"density_dpi": 420, "density_factor": 2.625}

    def test_get_density_malformed_returns_zero(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = "no number here"
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        assert ctrl.get_density("ABC") == {"density_dpi": 0, "density_factor": 0.0}

    def test_get_orientation(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = "  SurfaceOrientation: 1"
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        assert ctrl.get_orientation("ABC") == "landscape"

    def test_get_foreground_android15_no_m_prefix(self) -> None:
        """Lenovo Android 15 outputs `ResumedActivity:` (no `m` prefix).

        Improvement 977b7180 — current grep pattern misses this. Test passes
        the FULL dumpsys output (no shell-side pre-filter) so the controller's
        own pattern matching is exercised.
        """
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = (
            "ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)\n"
            "Display #0 (activities from top to bottom):\n"
            "  Task{abc #1 type=standard A=10000:com.lincmobile.peeklu U=0}\n"
            "    ResumedActivity: ActivityRecord{abc u0 com.lincmobile.peeklu/.MainActivity t45}\n"
            "      packageName=com.lincmobile.peeklu processName=com.lincmobile.peeklu\n"
        )
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        fg = ctrl.get_foreground("ABC")
        assert fg["package"] == "com.lincmobile.peeklu"
        assert fg["activity"] == ".MainActivity"

    def test_get_orientation_android15_viewport(self) -> None:
        """Lenovo Android 15 dumpsys input has no `SurfaceOrientation` line.

        Fall back to the `Viewport INTERNAL: ... orientation=N` line in
        `dumpsys input`. Improvement 977b7180.
        """
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = (
            "      Viewport INTERNAL: displayId=0, uniqueId=local:46, port=0, "
            "orientation=1, logicalFrame=[0, 0, 1920, 1200], physicalFrame=[0, 0, 1920, 1200], "
            "deviceSize=[1920, 1200], isActive=[0]"
        )
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        assert ctrl.get_orientation("ABC") == "landscape"

    def test_get_screen_size_swaps_when_landscape(self) -> None:
        """When device is in landscape, screen_size must report w>=h.

        `wm size` always returns native (portrait) dimensions. Improvement 977b7180
        — a landscape tablet should not report w=1200,h=1920.
        """
        ctrl = AndroidController()
        mock_device = MagicMock()
        # First shell call returns wm size, subsequent calls return orientation source.
        mock_device.shell.side_effect = [
            "Physical size: 1200x1920",
            "      Viewport INTERNAL: orientation=1, deviceSize=[1920, 1200]",
        ]
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        size = ctrl.get_screen_size("ABC")
        assert size == {"width": 1920, "height": 1200}

    def test_logcat(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = "line1\nline2\nline3\n"
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        output = ctrl.logcat("ABC", lines=10)
        assert "line1" in output

    def test_logcat_rejects_non_integer_lines_before_shell(self) -> None:
        """Audit 2026-09-30 F1: `lines` is cast with int() before reaching
        `logcat -d -t N`, so `"5 MARKER"` raises instead of being interpolated."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        with pytest.raises(ValueError):
            ctrl.logcat("ABC", lines="5 MARKER")  # type: ignore[arg-type]
        mock_device.shell.assert_not_called()

    def test_logcat_command_string_is_unchanged_for_int_lines(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = ""
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.logcat("ABC", lines=10)
        assert mock_device.shell.call_args[0][0] == "logcat -d -t 10"

    def test_logcat_tag_filter_drops_buffer_limit(self) -> None:
        """With a tag filter, `-t N` must be dropped from the device-side cmd.

        Android's `logcat -t N -s TAG` reads the last N raw buffer lines THEN
        filters by tag, so rare-tag events get dropped. The fix runs
        `logcat -d -s TAG` (no -t) and trims to N lines in Python.
        See improvement 40d8edf6.
        """
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = "tagline1\ntagline2\ntagline3\n"
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.logcat("ABC", lines=50, tag="MyTag")
        cmd = mock_device.shell.call_args.args[0]
        # Must NOT have -t N (which combined with -s reads raw buffer first)
        assert "-t " not in cmd, f"command still tail-limits buffer: {cmd!r}"
        assert "-s MyTag" in cmd

    def test_logcat_tag_filter_trims_lines_in_python(self) -> None:
        """Result must be trimmed to `lines` from the matching set."""
        ctrl = AndroidController()
        mock_device = MagicMock()
        # 200 matching lines come back from device
        mock_device.shell.return_value = "\n".join(f"line{i}" for i in range(200)) + "\n"
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        output = ctrl.logcat("ABC", lines=10, tag="MyTag")
        result_lines = [ln for ln in output.splitlines() if ln]
        assert len(result_lines) == 10
        # Last 10 of 200 → line190..line199
        assert result_lines[0] == "line190"
        assert result_lines[-1] == "line199"

    def test_get_log_stream_cmd(self) -> None:
        ctrl = AndroidController()
        cmd = ctrl.get_log_stream_cmd("ABC")
        assert cmd == [ADB_PATH, "-s", "ABC", "logcat", "-v", "threadtime"]

        cmd_with_tag = ctrl.get_log_stream_cmd("ABC", tag="MyApp")
        assert cmd_with_tag == [ADB_PATH, "-s", "ABC", "logcat", "-v", "threadtime", "-s", "MyApp"]

    @pytest.mark.parametrize("bad_tag", ["Tag; reboot", "`id`", "-c", "a b"])
    def test_logcat_rejects_hostile_tag(self, bad_tag: str) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        with pytest.raises(ValueError):
            ctrl.logcat("ABC", tag=bad_tag)
        mock_device.shell.assert_not_called()

    def test_logcat_valid_tag_sends_exact_command(self) -> None:
        ctrl = AndroidController()
        mock_device = MagicMock()
        mock_device.shell.return_value = "tagline1\n"
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl._client = mock_client

        ctrl.logcat("ABC", tag="MyTag-1.x")
        assert mock_device.shell.call_args.args[0] == "logcat -d -s MyTag-1.x"

    @pytest.mark.parametrize("bad_tag", ["Tag; reboot", "`id`", "-c", "a b"])
    def test_get_log_stream_cmd_rejects_hostile_tag(self, bad_tag: str) -> None:
        ctrl = AndroidController()
        with pytest.raises(ValueError):
            ctrl.get_log_stream_cmd("ABC", tag=bad_tag)


class TestCircuitBreaker:
    """Per-device circuit breaker tests.

    These tests mock _get_client to prevent adbutils reconnect from replacing
    the mock client when _client_errors >= 2 during repeated failures.
    """

    @staticmethod
    def _make_cb_controller(mock_client: MagicMock) -> AndroidController:
        """Create controller with stable mock client that survives reconnects."""
        ctrl = _make_controller()
        ctrl._client = mock_client
        ctrl._get_client = lambda: mock_client  # type: ignore[assignment]
        return ctrl

    def test_circuit_opens_after_3_failures(self) -> None:
        """3 consecutive failures on one device → DeviceError on next call."""
        import pytest

        mock_client = MagicMock()
        mock_client.device.return_value = MagicMock(shell=MagicMock(side_effect=OSError("gone")))
        ctrl = self._make_cb_controller(mock_client)

        # Each _shell call has 3 attempts (1 + 2 retries), each records a failure.
        # 1st call: 3 failures → circuit opens at failure #3.
        with pytest.raises(OSError):
            ctrl._shell("A", "echo hi")

        # Circuit is now open — next call should raise DeviceError immediately
        call_count_before = mock_client.device.call_count
        with pytest.raises(DeviceError, match="circuit open"):
            ctrl._shell("A", "echo hi")
        # No ADB call was made (device() not called again)
        assert mock_client.device.call_count == call_count_before

    def test_circuit_closes_on_success(self) -> None:
        """Success clears failure count so circuit doesn't open prematurely."""
        call_count = 0

        def shell_side_effect(cmd: str, **kw: object) -> str:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise OSError("flaky")
            return "ok"

        mock_device = MagicMock()
        mock_device.shell = MagicMock(side_effect=shell_side_effect)
        mock_client = MagicMock()
        mock_client.device.return_value = mock_device
        ctrl = self._make_cb_controller(mock_client)

        # This call: 2 failures then success → failure count cleared
        result = ctrl._shell("A", "echo hi")
        assert result == "ok"
        assert "A" not in ctrl._circuit_breakers

    def test_adb_restart_requires_multi_device_failure(self) -> None:
        """ADB restart only when 3+ distinct devices fail within window."""
        import time

        import pytest

        from linc_nerve.core import adb_health
        from linc_nerve.core.adb_health import AdbTransportStatus

        status = AdbTransportStatus(
            responsive=False,
            wedged=True,
            device_count=None,
            probe_seconds=8.0,
            server_pid=1234,
            server_cpu_percent=99.0,
            detail="busy_spin",
            checked_at=time.time(),
        )
        adb_health.record_transport_status(status)
        try:
            mock_client = MagicMock()
            mock_client.device.return_value = MagicMock(
                shell=MagicMock(side_effect=OSError("gone"))
            )
            ctrl = self._make_cb_controller(mock_client)

            with patch.object(ctrl, "_restart_adb_server") as mock_restart:
                # Fail on device A — no restart
                with pytest.raises(OSError):
                    ctrl._shell("A", "echo hi")
                assert mock_restart.call_count == 0

                # Fail on device B — still only 2 distinct devices
                with pytest.raises(OSError):
                    ctrl._shell("B", "echo hi")
                assert mock_restart.call_count == 0

                # Fail on device C — 3 distinct devices → restart triggered
                with pytest.raises(OSError):
                    ctrl._shell("C", "echo hi")
                assert mock_restart.call_count >= 1
        finally:
            adb_health.reset_transport_status()

    def test_cooldown_expiry(self) -> None:
        """After cooldown expires, device can be used again."""
        import time

        import pytest

        mock_client = MagicMock()
        mock_client.device.return_value = MagicMock(shell=MagicMock(side_effect=OSError("gone")))
        ctrl = self._make_cb_controller(mock_client)

        # Open circuit
        with pytest.raises(OSError):
            ctrl._shell("A", "echo hi")

        # Verify circuit is open
        with pytest.raises(DeviceError):
            ctrl._shell("A", "echo hi")

        # Expire the cooldown manually
        ctrl._circuit_breakers["A"].degraded_until = time.monotonic() - 1

        # Now make device work
        mock_client.device.return_value = MagicMock(shell=MagicMock(return_value="hello"))
        result = ctrl._shell("A", "echo hi")
        assert result == "hello"
        assert "A" not in ctrl._circuit_breakers

    def test_circuit_isolation(self) -> None:
        """Circuit open on device A doesn't affect device B."""
        import pytest

        failing_device = MagicMock(shell=MagicMock(side_effect=OSError("gone")))
        working_device = MagicMock(shell=MagicMock(return_value="ok"))

        mock_client = MagicMock()
        mock_client.device.return_value = failing_device
        ctrl = self._make_cb_controller(mock_client)

        # Open circuit on A
        with pytest.raises(OSError):
            ctrl._shell("A", "echo hi")

        # B should still work
        mock_client.device.return_value = working_device
        result = ctrl._shell("B", "echo hi")
        assert result == "ok"

        # A is still degraded
        from linc_nerve.core.android import CircuitState

        assert "A" in ctrl._circuit_breakers
        assert ctrl._circuit_breakers["A"].state == CircuitState.OPEN
        with pytest.raises(DeviceError):
            ctrl._shell("A", "echo hi")

    def test_state_transitions(self) -> None:
        """Verify CLOSED → OPEN → HALF_OPEN → CLOSED lifecycle."""
        import time

        import pytest

        from linc_nerve.core.android import CircuitState

        mock_client = MagicMock()
        mock_client.device.return_value = MagicMock(shell=MagicMock(side_effect=OSError("gone")))
        ctrl = self._make_cb_controller(mock_client)

        # CLOSED → OPEN (3 failures trip the circuit)
        with pytest.raises(OSError):
            ctrl._shell("A", "echo hi")
        cb = ctrl._circuit_breakers["A"]
        assert cb.state == CircuitState.OPEN

        # OPEN → check rejects
        with pytest.raises(DeviceError):
            ctrl._shell("A", "echo hi")

        # Expire cooldown → HALF_OPEN on next check
        cb.degraded_until = time.monotonic() - 1
        mock_client.device.return_value = MagicMock(shell=MagicMock(return_value="ok"))
        result = ctrl._shell("A", "echo hi")
        assert result == "ok"
        # Success clears breaker entirely → back to CLOSED (no entry)
        assert "A" not in ctrl._circuit_breakers
