"""Request-path device resolution must not pay for a full fleet rediscovery.

linc-nerve-fv0: ``nerve elements`` (~13 s) and ``nerve key BACK`` (~12 s) on
the Galaxy Tab Active3, while the controller itself took ~3.3 s / <2 s. The
time outside the controller was ``_core._resolve_device``: whenever the 5 s
device cache had expired (any agent pause between calls), it ran
``_refresh_cache`` = ``android.discover()`` — three ``getprop`` shells per
connected device, each under that device's ``_device_lock`` (so it queues
behind another lane's ``uiautomator dump``) — plus ``ios.discover()``.

The fix resolves a serial the (stale) cache already knows with one cheap
``host:devices`` liveness check instead. Everything is driven through seams:
fake controllers on ``_core._android`` / ``_core._ios``, a fake clock on
``_core._clock``, and the cache globals — no adb, no daemon.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from linc_nerve.core.device import DeviceType, NerveDevice, Platform
from linc_nerve.mcp import _core
from linc_nerve.mcp.input_tools import _nerve_key_sync, _nerve_tap_sync
from linc_nerve.mcp.ui_tools import _nerve_elements_sync


def _tab() -> NerveDevice:
    return NerveDevice(
        serial="S",
        platform=Platform.ANDROID,
        device_type=DeviceType.TABLET,
        model="SM-T570",
        os_version="12",
    )


def _iphone() -> NerveDevice:
    return NerveDevice(
        serial="IPHONE",
        platform=Platform.IOS,
        device_type=DeviceType.PHONE,
        model="iPhone",
        os_version="18",
    )


class _FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FakeAndroid:
    """Records discovery work; ``ready`` is the adb ``device``-state set."""

    def __init__(self, devices: list[NerveDevice], ready: set[str]) -> None:
        self.devices = devices
        self.ready = ready
        self.discover_calls = 0
        self.ready_checks: list[str] = []
        self.keys: list[tuple[str, str]] = []

    def discover(self) -> list[NerveDevice]:
        self.discover_calls += 1
        return list(self.devices)

    def is_ready_serial(self, serial: str) -> bool:
        self.ready_checks.append(serial)
        return serial in self.ready

    def press_key(self, serial: str, key: str) -> None:
        self.keys.append((serial, key))

    def tap(self, serial: str, x: int, y: int) -> None:
        pass

    def elements(self, serial: str) -> list[Any]:
        return []


class _FakeIOS:
    def __init__(self, devices: list[NerveDevice] | None = None) -> None:
        self.devices = devices or []
        self.discover_calls = 0

    def discover(self) -> list[NerveDevice]:
        self.discover_calls += 1
        return list(self.devices)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(_core, "_clock", fake)
    return fake


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch, clock: _FakeClock) -> Any:
    """Install fake controllers + a cache populated at ``clock.now``."""

    def _wire(
        android: _FakeAndroid, ios: _FakeIOS, cached: list[NerveDevice]
    ) -> tuple[_FakeAndroid, _FakeIOS]:
        device_mgr = MagicMock()
        device_mgr.guard.return_value = None
        monkeypatch.setattr(_core, "_android", android)
        monkeypatch.setattr(_core, "_ios", ios)
        monkeypatch.setattr(_core, "_state_mgr", MagicMock())
        monkeypatch.setattr(_core, "_metrics", MagicMock())
        monkeypatch.setattr(_core, "_device_mgr", device_mgr)
        monkeypatch.setattr(_core, "_emulator", MagicMock())
        monkeypatch.setattr(_core, "_cached_devices", list(cached))
        monkeypatch.setattr(_core, "_cache_timestamp", clock.now)
        return android, ios

    return _wire


class TestStaleCacheResolution:
    def test_key_back_on_known_serial_skips_rediscovery(
        self, wire: Any, clock: _FakeClock
    ) -> None:
        """Two key BACK calls 6 s apart: no android/iOS discover on the request path."""
        android, ios = wire(_FakeAndroid([_tab()], {"S"}), _FakeIOS(), [_tab()])

        clock.now += 6.0  # past _CACHE_TTL
        assert _nerve_key_sync("S", "BACK") == {"success": True, "action": "key", "key": "BACK"}
        clock.now += 6.0
        assert _nerve_key_sync("S", "BACK")["success"] is True

        assert ios.discover_calls == 0
        assert android.discover_calls == 0
        assert android.ready_checks == ["S", "S"]
        assert android.keys == [("S", "BACK"), ("S", "BACK")]

    def test_elements_on_known_serial_skips_rediscovery(
        self, wire: Any, clock: _FakeClock
    ) -> None:
        android, ios = wire(_FakeAndroid([_tab()], {"S"}), _FakeIOS(), [_tab()])

        clock.now += 6.0
        result = _nerve_elements_sync("S")

        assert "error" not in result
        assert ios.discover_calls == 0
        assert android.discover_calls == 0

    def test_fresh_cache_needs_no_liveness_check(self, wire: Any, clock: _FakeClock) -> None:
        android, _ = wire(_FakeAndroid([_tab()], {"S"}), _FakeIOS(), [_tab()])

        clock.now += 1.0  # within _CACHE_TTL
        _nerve_tap_sync("S", 10, 20)

        assert android.ready_checks == []
        assert android.discover_calls == 0

    def test_stale_serial_no_longer_ready_falls_back_to_full_refresh(
        self, wire: Any, clock: _FakeClock
    ) -> None:
        """A device that left adb is not resolved from the stale cache."""
        android, ios = wire(_FakeAndroid([], set()), _FakeIOS(), [_tab()])

        clock.now += 6.0
        result = _nerve_key_sync("S", "BACK")

        assert "not found" in result["error"]
        assert android.discover_calls >= 1
        assert ios.discover_calls >= 1
        assert android.keys == []

    def test_liveness_check_error_falls_back_to_full_refresh(
        self, wire: Any, clock: _FakeClock
    ) -> None:
        android, ios = wire(_FakeAndroid([_tab()], {"S"}), _FakeIOS(), [_tab()])

        def _boom(serial: str) -> bool:
            raise ConnectionError("adb server gone")

        android.is_ready_serial = _boom  # type: ignore[method-assign]
        clock.now += 6.0
        assert _nerve_key_sync("S", "BACK")["success"] is True

        assert android.discover_calls == 1
        assert ios.discover_calls == 1

    def test_stale_ios_serial_still_refreshes(self, wire: Any, clock: _FakeClock) -> None:
        """iOS has no cheap liveness probe — its resolution is unchanged."""
        android, ios = wire(_FakeAndroid([], set()), _FakeIOS([_iphone()]), [_iphone()])

        clock.now += 6.0
        dev, platform = _core._resolve_device("IPHONE")

        assert dev is not None and platform == "ios"
        assert ios.discover_calls == 1
        assert android.ready_checks == []

    def test_explicit_invalidate_forces_full_refresh(self, wire: Any, clock: _FakeClock) -> None:
        """``_invalidate_cache()`` is never answered from the stale entry.

        The emulator tools invalidate so a restarted AVD that reuses
        ``emulator-5554`` is re-read; the cheap liveness check would otherwise
        hand back the old entry (adb lists the serial again) for its model/type.
        """
        old = NerveDevice(
            serial="emulator-5554",
            platform=Platform.ANDROID,
            device_type=DeviceType.EMULATOR,
            model="sdk_gphone64_arm64 (old AVD)",
            os_version="14",
        )
        new = NerveDevice(
            serial="emulator-5554",
            platform=Platform.ANDROID,
            device_type=DeviceType.EMULATOR,
            model="sdk_gphone64_arm64 (restarted AVD)",
            os_version="15",
        )
        android, ios = wire(_FakeAndroid([new], {"emulator-5554"}), _FakeIOS(), [old])

        _core._invalidate_cache()
        dev, platform = _core._resolve_device("emulator-5554")

        assert android.discover_calls == 1
        assert ios.discover_calls == 1
        assert android.ready_checks == []
        assert dev is not None and platform == "android"
        assert dev.model == "sdk_gphone64_arm64 (restarted AVD)"

    def test_invalidate_long_after_refresh_still_forces_full_refresh(
        self, wire: Any, clock: _FakeClock
    ) -> None:
        """Invalidation wins even when the TTL has also expired."""
        android, ios = wire(_FakeAndroid([_tab()], {"S"}), _FakeIOS(), [_tab()])

        clock.now += 60.0
        _core._invalidate_cache()
        assert _nerve_key_sync("S", "BACK")["success"] is True

        assert android.discover_calls == 1
        assert ios.discover_calls == 1
        assert android.ready_checks == []

    def test_unknown_serial_still_refreshes(self, wire: Any, clock: _FakeClock) -> None:
        android, ios = wire(_FakeAndroid([_tab()], {"S"}), _FakeIOS(), [])

        dev, platform = _core._resolve_device("S")

        assert dev is not None and platform == "android"
        assert android.discover_calls == 1
        assert ios.discover_calls == 1
        assert android.ready_checks == []


class TestRefreshTiming:
    def test_refresh_emits_debug_duration(self, wire: Any, clock: _FakeClock) -> None:
        android, _ = wire(_FakeAndroid([_tab()], {"S"}), _FakeIOS([_iphone()]), [])

        def _slow_discover() -> list[NerveDevice]:
            clock.now += 2.5
            return [_tab()]

        android.discover = _slow_discover  # type: ignore[method-assign]
        with capture_logs() as logs:
            _core._refresh_cache()

        events = [e for e in logs if e["event"] == "device_cache_refreshed"]
        assert len(events) == 1
        assert events[0]["log_level"] == "debug"
        assert events[0]["duration_ms"] == pytest.approx(2500.0)
        assert events[0]["device_count"] == 2
        assert _core._cache_timestamp == clock.now


class TestAndroidReadySerial:
    def test_is_ready_serial_reads_host_devices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from linc_nerve.core import android as android_mod

        ctrl = android_mod.AndroidController()
        monkeypatch.setattr(ctrl, "_get_client", lambda: object())
        monkeypatch.setattr(android_mod, "_ready_adb_serials", lambda client: ("S", "OTHER"))

        assert ctrl.is_ready_serial("S") is True
        assert ctrl.is_ready_serial("GONE") is False


class TestIdleStateDumpFailure:
    """With Just Player paused on its controller overlay (Tab Active3,
    measured 10/10), ``uiautomator dump`` waits its fixed on-device idle
    window and prints ``ERROR: could not get idle state.`` — the screen never
    went idle. That is NOT a leaked UiAutomation holder, so the error must
    say so (the old text sent agents to reboot the device)."""

    @staticmethod
    def _ctrl(outputs: list[str]) -> tuple[Any, list[str]]:
        from linc_nerve.core.android import AndroidController

        ctrl = AndroidController()
        calls: list[str] = []
        device = MagicMock()

        def _shell(cmd: str, **_: Any) -> str:
            calls.append(cmd)
            return outputs.pop(0) if cmd.startswith("uiautomator dump") else ""

        device.shell.side_effect = _shell
        client = MagicMock()
        client.device.return_value = device
        ctrl._client = client
        return ctrl, calls

    def test_idle_timeout_raises_named_cause_never_empty(self) -> None:
        from linc_nerve.core.android_ui import UiHierarchyUnavailableError

        ctrl, calls = self._ctrl(["ERROR: could not get idle state.\n"])

        with pytest.raises(UiHierarchyUnavailableError) as exc_info:
            ctrl.elements("S")

        msg = str(exc_info.value)
        assert "never went idle" in msg
        assert "could not get idle state" in msg
        assert "reboot" not in msg
        # The leaked-holder self-heal still runs first (unchanged): one ps scan.
        assert "ps -A -o NAME" in calls

    def test_other_non_xml_output_keeps_holder_remedy(self) -> None:
        from linc_nerve.core.android_ui import UiHierarchyUnavailableError

        ctrl, _ = self._ctrl(["Killed\n"])

        with pytest.raises(UiHierarchyUnavailableError) as exc_info:
            ctrl.elements("S")

        assert "UiAutomation" in str(exc_info.value)
        assert "never went idle" not in str(exc_info.value)
